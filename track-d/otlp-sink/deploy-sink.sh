#!/usr/bin/env bash
# Requirement 6 - deploy a non-Azure-style OTLP receiver into the VNet.
#
# Stands in for DocuSign's own observability backend so the question "can the
# agent export telemetry somewhere Microsoft does not own" can be measured
# rather than quoted from documentation.
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

APP="${SINK_APP:-otlp-sink}"
IMAGE="${SINK_IMAGE:-mcr.microsoft.com/azurelinux/base/python:3.12}"
ENV_ID="${SINK_ACA_ENV:?set SINK_ACA_ENV}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
CODE_B64="$(base64 -w0 "${SCRIPT_DIR}/otlp_sink.py")"

CODE_B64="${CODE_B64}" APP="${APP}" IMAGE="${IMAGE}" ENV_ID="${ENV_ID}" \
python3 - "${WORK}/app.yaml" <<'PYEOF'
import os, sys, yaml
doc = {
    "location": os.environ.get("SINK_LOCATION", "eastus2"),
    "properties": {
        "environmentId": os.environ["ENV_ID"],
        "configuration": {
            "ingress": {
                "external": True, "targetPort": 4318, "transport": "http",
                "allowInsecure": False,
            },
        },
        "template": {
            "containers": [{
                "name": os.environ["APP"],
                "image": os.environ["IMAGE"],
                "command": ["/bin/sh", "-c"],
                "args": ['echo "$SINK_CODE_B64" | base64 -d > /tmp/sink.py && exec python3 /tmp/sink.py'],
                "resources": {"cpu": 0.5, "memory": "1Gi"},
                "env": [
                    {"name": "SINK_CODE_B64", "value": os.environ["CODE_B64"]},
                    {"name": "PORT", "value": "4318"},
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
echo "==> OTLP endpoint: https://${FQDN}"
echo "==> set AGENTENV_OTEL_EXPORTER_OTLP_ENDPOINT=https://${FQDN}"
