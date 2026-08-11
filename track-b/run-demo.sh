#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/_env.sh"
require_env RG_SECURE JOB_PRIVATE_API

resource_group="$RG_SECURE"
job_name="$JOB_PRIVATE_API"

execution_name="$(
  az containerapp job start \
    --resource-group "$resource_group" \
    --name "$job_name" \
    --only-show-errors \
    --query name \
    --output tsv
)"

echo "Started execution: $execution_name"

for _ in $(seq 1 45); do
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
      echo "Execution failed with status: $status" >&2
      exit 1
      ;;
  esac
  sleep 10
done

if [[ "${status:-}" != "Succeeded" ]]; then
  echo "Timed out waiting for the test job" >&2
  exit 1
fi

az containerapp job logs show \
  --resource-group "$resource_group" \
  --name "$job_name" \
  --execution "$execution_name" \
  --container "$job_name" \
  --tail 100 \
  --format text
