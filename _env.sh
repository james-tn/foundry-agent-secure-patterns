#!/usr/bin/env bash
# Shared config loader for the demo runners.
#
# Source this, then call require_env with the variable names the caller needs:
#     source "$(dirname "${BASH_SOURCE[0]}")/../_env.sh"
#     require_env RG_SECURE JOB_PRIVATE_API
#
# Reads deployment identifiers from env.local (see env.local.example).
# Override the location with ENV_FILE=/path/to/config.

_ENV_SH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_ENV_SH_CONF="${ENV_FILE:-$_ENV_SH_ROOT/env.local}"

if [[ ! -f "$_ENV_SH_CONF" ]]; then
  echo "Missing config: $_ENV_SH_CONF" >&2
  echo "Create it from the template:  cp env.local.example env.local" >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$_ENV_SH_CONF"

require_env() {
  local missing=() v
  for v in "$@"; do
    [[ -z "${!v:-}" ]] && missing+=("$v")
  done
  if (( ${#missing[@]} )); then
    echo "Unset values in $_ENV_SH_CONF: ${missing[*]}" >&2
    exit 2
  fi
}
