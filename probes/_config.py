"""Shared config loader for the measurement probes.

Reads deployment identifiers from env.local at the repo root so that no
subscription or resource names are hard-coded in source.
See env.local.example for the template.
"""
import os
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_ENV_FILE = pathlib.Path(os.environ.get("ENV_FILE", _ROOT / "env.local"))


def _load():
    values = {}
    if _ENV_FILE.is_file():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith(("AZ_", "RG_", "POOL_", "JOB_", "OAI_", "FOUNDRY_", "INTERNAL_"))})
    return values


_CONF = _load()


def get(name, default=None, required=True):
    val = _CONF.get(name, default)
    if required and not val:
        raise SystemExit(
            f"Missing '{name}'. Create {_ENV_FILE.name} from env.local.example "
            f"(or set {name} in the environment)."
        )
    return val
