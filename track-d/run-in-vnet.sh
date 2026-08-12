#!/usr/bin/env bash
# Track D - run a Track D script inside the VNet via the ACA job.
#
# The Foundry data plane is unreachable from outside the VNet, so every
# hosted-agent operation (deploy, inspect, invoke) has to run here.
#
# Usage: ./run-in-vnet.sh <script.py>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/../_env.sh"
require_env RG_SECURE

# Pin the subscription for every az call in this script. The CLI keeps a single
# mutable "active subscription" in a shared profile, so any other shell that
# runs `az account set` silently retargets these commands and they fail with a
# confusing ResourceGroupNotFound.
if [ -n "${AZ_SUBSCRIPTION:-}" ]; then
  az() { command az "$@" --subscription "${AZ_SUBSCRIPTION}"; }
fi

ENTRY="${1:-deploy_agent.py}"
shift || true
ENV_OVERRIDES=("$@")   # optional KEY=VALUE pairs applied to the job
JOB="${JOB_TRACKD:-trackd-deploy}"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "==> packaging payload (entry: ${ENTRY})"
mkdir -p "${WORK}/payload"
cp "${SCRIPT_DIR}"/*.py "${WORK}/payload/"
cp "${SCRIPT_DIR}/../probes/_config.py" "${WORK}/payload/"
for d in "${SCRIPT_DIR}"/agent-src*; do cp -r "$d" "${WORK}/payload/$(basename "$d")"; done
tar czf "${WORK}/p.tgz" -C "${WORK}/payload" .
B64="$(base64 -w0 "${WORK}/p.tgz")"

echo "==> updating job ${JOB}"
az containerapp job show -g "${RG_SECURE}" -n "${JOB}" -o yaml > "${WORK}/job.yaml" 2>/dev/null
python3 - "${WORK}/job.yaml" "${B64}" "${ENTRY}" "${ENV_OVERRIDES[@]}" <<'PYEOF'
import sys, yaml
path, b64, entry = sys.argv[1], sys.argv[2], sys.argv[3]
overrides = dict(pair.split("=", 1) for pair in sys.argv[4:] if "=" in pair)
overrides["PAYLOAD_B64"] = b64
overrides["ENTRY"] = entry

doc = yaml.safe_load(open(path))
container = doc["properties"]["template"]["containers"][0]
env = {e["name"]: e for e in container.setdefault("env", [])}
for key, value in overrides.items():
    if key in env:
        env[key]["value"] = value
    else:
        container["env"].append({"name": key, "value": value})
yaml.safe_dump(doc, open(path, "w"), default_flow_style=False, width=10**6)
PYEOF
az containerapp job update -g "${RG_SECURE}" -n "${JOB}" --yaml "${WORK}/job.yaml" -o none

echo "==> starting"
EXEC="$(az containerapp job start -g "${RG_SECURE}" -n "${JOB}" -o tsv --query name)"
echo "    execution: ${EXEC}"

for _ in $(seq 1 "${POLL_TICKS:-60}"); do
  STATUS="$(az containerapp job execution show -g "${RG_SECURE}" -n "${JOB}" \
    --job-execution-name "${EXEC}" -o tsv --query properties.status 2>/dev/null || echo Unknown)"
  case "${STATUS}" in Succeeded|Failed) break ;; esac
  sleep 10
done
echo "==> status: ${STATUS}"

echo "==> logs"
az containerapp job logs show -g "${RG_SECURE}" -n "${JOB}" --container "${JOB}" \
  --execution "${EXEC}" --tail 200 2>/dev/null \
  | grep -o '"Log":"[^"]*"' | sed 's/"Log":"//;s/"$//' | grep -v 'Connect' || true

[ "${STATUS}" = "Succeeded" ]
