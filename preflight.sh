#!/usr/bin/env bash
# Pre-flight check. Run 30 minutes before the session.
# Verifies identity, subscription, and every resource the demos touch.
#
# Reads deployment identifiers from env.local (see env.local.example).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${ENV_FILE:-$HERE/env.local}"

if [[ ! -f "$CONF" ]]; then
  echo "Missing config: $CONF"
  echo "Create it from the template:  cp env.local.example env.local"
  exit 2
fi
# shellcheck disable=SC1090
source "$CONF"

missing=()
for v in AZ_SUBSCRIPTION RG_SECURE RG_LATENCY FOUNDRY_ACCOUNT INTERNAL_API_APP \
         POOL_CONTROLLED POOL_BASELINE POOL_COLD POOL_WARM \
         JOB_PRIVATE_API JOB_CODE_TEST OAI_RG OAI_ACCOUNT; do
  [[ -z "${!v:-}" ]] && missing+=("$v")
done
if (( ${#missing[@]} )); then
  echo "Unset values in $CONF: ${missing[*]}"
  exit 2
fi

fail=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }

echo "== 1. Azure identity and subscription =="
current="$(az account show --query id -o tsv 2>/dev/null)"
if [[ "$current" != "$AZ_SUBSCRIPTION" ]]; then
  echo "  Wrong/missing subscription ($current). Switching..."
  az account set --subscription "$AZ_SUBSCRIPTION" 2>/dev/null
  current="$(az account show --query id -o tsv 2>/dev/null)"
fi
[[ "$current" == "$AZ_SUBSCRIPTION" ]] && ok "subscription selected" \
  || bad "could not select the configured subscription"
user="$(az account show --query user.name -o tsv 2>/dev/null)"
if [[ -n "${EXPECTED_USER_DOMAIN:-}" ]]; then
  [[ "$user" == *"$EXPECTED_USER_DOMAIN" ]] && ok "signed in as $user" \
    || bad "unexpected identity: $user (run: az login)"
else
  ok "signed in as $user"
fi

echo "== 2. Foundry account (private) =="
state="$(az cognitiveservices account show -g "$RG_SECURE" -n "$FOUNDRY_ACCOUNT" --query properties.provisioningState -o tsv 2>/dev/null)"
pna="$(az cognitiveservices account show -g "$RG_SECURE" -n "$FOUNDRY_ACCOUNT" --query properties.publicNetworkAccess -o tsv 2>/dev/null)"
[[ "$state" == "Succeeded" ]] && ok "Foundry account: $state" || bad "Foundry account: ${state:-missing}"
[[ "$pna" == "Disabled" ]] && ok "public network access: Disabled" \
  || bad "public network access is $pna (expected Disabled)"

echo "== 3. Private internal API =="
st="$(az containerapp show -g "$RG_SECURE" -n "$INTERNAL_API_APP" --query properties.provisioningState -o tsv 2>/dev/null)"
run="$(az containerapp show -g "$RG_SECURE" -n "$INTERNAL_API_APP" --query properties.runningStatus -o tsv 2>/dev/null)"
[[ "$run" == "Running" ]] && ok "internal API: $run" || bad "internal API: ${st:-missing}/${run:-}"

echo "== 4. Session pools =="
for p in "$POOL_CONTROLLED" "$POOL_BASELINE"; do
  s="$(az containerapp sessionpool show -g "$RG_SECURE" -n "$p" --query properties.provisioningState -o tsv 2>/dev/null)"
  [[ "$s" == "Succeeded" ]] && ok "$p: $s" || bad "$p: ${s:-missing}"
done
for p in "$POOL_COLD" "$POOL_WARM"; do
  s="$(az containerapp sessionpool show -g "$RG_LATENCY" -n "$p" --query properties.provisioningState -o tsv 2>/dev/null)"
  [[ "$s" == "Succeeded" ]] && ok "$p: $s" || bad "$p: ${s:-missing}"
done

echo "== 5. Demo jobs and images =="
for j in "$JOB_PRIVATE_API" "$JOB_CODE_TEST"; do
  img="$(az containerapp job show -g "$RG_SECURE" -n "$j" --query "properties.template.containers[0].image" -o tsv 2>/dev/null)"
  [[ -n "$img" ]] && ok "$j -> $img" || bad "$j missing"
done

echo "== 6. No stray PTU deployments (cost guard) =="
ptu="$(az cognitiveservices account deployment list -g "$OAI_RG" -n "$OAI_ACCOUNT" \
  --query "[?contains(sku.name,'Provisioned')].name" -o tsv 2>/dev/null)"
[[ -z "$ptu" ]] && ok "no provisioned deployments billing" || bad "PTU STILL DEPLOYED: $ptu"

echo
if [[ $fail -eq 0 ]]; then
  echo "PRE-FLIGHT PASSED. Both demos are ready."
else
  echo "PRE-FLIGHT FAILED with $fail problem(s). Fix before the session."
fi
exit $fail
