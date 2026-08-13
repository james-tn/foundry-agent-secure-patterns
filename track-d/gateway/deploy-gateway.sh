#!/usr/bin/env bash
# Requirement 5 - deploy the POC multi-provider LLM gateway into the VNet.
#
# The gateway is stdlib-only, so it runs on a stock python image with no build
# step. The private ACR cannot be built against from outside the VNet, so the
# source is injected as a base64 env var and decoded at start-up instead of
# being baked into an image.
#
# YAML rather than flags: the container `command`/`args` vectors are the one
# part of the ACA CLI surface that does not round-trip reliably through flags.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/../../_env.sh"
require_env RG_SECURE

if [ -n "${AZ_SUBSCRIPTION:-}" ]; then
  az() { command az "$@" --subscription "${AZ_SUBSCRIPTION}"; }
fi

APP="${GW_APP:-llm-gateway}"
IMAGE="${GW_IMAGE:-mcr.microsoft.com/azurelinux/base/python:3.12}"
AOAI_ENDPOINT="${GW_AOAI_ENDPOINT:?set GW_AOAI_ENDPOINT}"
IDENTITY_ID="${GW_IDENTITY_ID:?set GW_IDENTITY_ID}"
IDENTITY_CLIENT_ID="${GW_IDENTITY_CLIENT_ID:?set GW_IDENTITY_CLIENT_ID}"
ENV_ID="${GW_ACA_ENV:?set GW_ACA_ENV}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
CODE_B64="$(base64 -w0 "${SCRIPT_DIR}/gateway.py")"

CODE_B64="${CODE_B64}" APP="${APP}" IMAGE="${IMAGE}" ENV_ID="${ENV_ID}" \
IDENTITY_ID="${IDENTITY_ID}" IDENTITY_CLIENT_ID="${IDENTITY_CLIENT_ID}" \
AOAI_ENDPOINT="${AOAI_ENDPOINT}" python3 - "${WORK}/app.yaml" <<'PYEOF'
import os, sys, yaml
doc = {
    "location": os.environ.get("GW_LOCATION", "eastus2"),
    "identity": {
        "type": "UserAssigned",
        "userAssignedIdentities": {os.environ["IDENTITY_ID"]: {}},
    },
    "properties": {
        "environmentId": os.environ["ENV_ID"],
        "configuration": {
            "ingress": {
                "external": True, "targetPort": 8080, "transport": "http",
                "allowInsecure": False,
            },
        },
        "template": {
            "containers": [{
                "name": os.environ["APP"],
                "image": os.environ["IMAGE"],
                "command": ["/bin/sh", "-c"],
                "args": ['echo "$GW_CODE_B64" | base64 -d > /tmp/gw.py && exec python3 /tmp/gw.py'],
                "resources": {"cpu": 0.5, "memory": "1Gi"},
                "env": [
                    {"name": "GW_CODE_B64", "value": os.environ["CODE_B64"]},
                    {"name": "AOAI_ENDPOINT", "value": os.environ["AOAI_ENDPOINT"]},
                    {"name": "GW_CLIENT_ID", "value": os.environ["IDENTITY_CLIENT_ID"]},
                    {"name": "PYTHONUNBUFFERED", "value": "1"},
                ],
            }],
            # Pinned to a single always-on replica: the audit log is in-memory,
            # so scaling would split the evidence across replicas.
            "scale": {"minReplicas": 1, "maxReplicas": 1},
        },
    },
}
yaml.safe_dump(doc, open(sys.argv[1], "w"), default_flow_style=False, width=10**6)
PYEOF

if az containerapp show -g "${RG_SECURE}" -n "${APP}" -o none 2>/dev/null; then
  echo "==> updating ${APP}"
  az containerapp update -g "${RG_SECURE}" -n "${APP}" --yaml "${WORK}/app.yaml" -o none
else
  echo "==> creating ${APP}"
  az containerapp create -g "${RG_SECURE}" -n "${APP}" --yaml "${WORK}/app.yaml" -o none
fi

FQDN="$(az containerapp show -g "${RG_SECURE}" -n "${APP}" -o tsv \
  --query properties.configuration.ingress.fqdn)"
echo "==> gateway base url: https://${FQDN}/v1"
