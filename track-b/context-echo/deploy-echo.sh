#!/usr/bin/env bash
# Requirement 4 - deploy the downstream-context echo API into the VNet.
#
# A prompt agent calls this through an OpenAPI tool. It records every header it
# receives, so "what context reaches an internal API" is answered from the wire
# rather than from the model's summary of it.
#
# Same constraints as the LLM gateway: stdlib-only source injected as base64,
# stock python image, no build step, YAML rather than flags.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/../../_env.sh"
require_env RG_SECURE

if [ -n "${AZ_SUBSCRIPTION:-}" ]; then
  az() { command az "$@" --subscription "${AZ_SUBSCRIPTION}"; }
fi

APP="${ECHO_APP:-context-echo}"
IMAGE="${ECHO_IMAGE:-mcr.microsoft.com/azurelinux/base/python:3.12}"
ENV_ID="${ECHO_ACA_ENV:?set ECHO_ACA_ENV}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
CODE_B64="$(base64 -w0 "${SCRIPT_DIR}/context_echo.py")"

CODE_B64="${CODE_B64}" APP="${APP}" IMAGE="${IMAGE}" ENV_ID="${ENV_ID}" \
python3 - "${WORK}/app.yaml" <<'PYEOF'
import os, sys, yaml
doc = {
    "location": os.environ.get("ECHO_LOCATION", "eastus2"),
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
                "args": ['echo "$ECHO_CODE_B64" | base64 -d > /tmp/echo.py && exec python3 /tmp/echo.py'],
                "resources": {"cpu": 0.5, "memory": "1Gi"},
                "env": [
                    {"name": "ECHO_CODE_B64", "value": os.environ["CODE_B64"]},
                    {"name": "PORT", "value": "8080"},
                    {"name": "PYTHONUNBUFFERED", "value": "1"},
                ],
            }],
            # One always-on replica: the received-telemetry log is in memory,
            # so scaling would split the evidence.
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
# The spec advertises its own server URL, which is only knowable after create.
az containerapp update -g "${RG_SECURE}" -n "${APP}" -o none \
  --set-env-vars "PUBLIC_URL=https://${FQDN}"

echo "==> echo API: https://${FQDN}"
echo "==> spec:     https://${FQDN}/openapi.json"
echo "==> received: https://${FQDN}/_last"
