#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/_env.sh"
require_env RG_SECURE JOB_CODE_TEST

resource_group="$RG_SECURE"
job_name="$JOB_CODE_TEST"

execution_name="$(
  az containerapp job start \
    --resource-group "$resource_group" \
    --name "$job_name" \
    --only-show-errors \
    --query name \
    --output tsv
)"

echo "Started execution: $execution_name"

for _ in $(seq 1 120); do
  status="$(
    az containerapp job execution show \
      --resource-group "$resource_group" \
      --name "$job_name" \
      --job-execution-name "$execution_name" \
      --query properties.status \
      --output tsv
  )"
  echo "Status: $status"
  case "$status" in
    Succeeded)
      break
      ;;
    Failed|Stopped|Degraded)
      break
      ;;
  esac
  sleep 10
done

az containerapp job logs show \
  --resource-group "$resource_group" \
  --name "$job_name" \
  --execution "$execution_name" \
  --container "$job_name" \
  --tail 300 \
  --format text

if [[ "$status" != "Succeeded" ]]; then
  echo "Execution failed with status: $status" >&2
  exit 1
fi
