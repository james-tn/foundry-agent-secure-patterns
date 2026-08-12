"""Track D - inspect hosted agents from inside the VNet.

The agents surface is data-plane only (ARM returns UnsupportedAction), so this
has to run in-VNet just like the deployment does.
"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "probes"))
import _config  # noqa: E402

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402

import os

AGENT_FOCUS = os.environ.get("TRACKD_AGENT_NAME", "trackd-langgraph")


def _flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = str(d).replace('"', "'")[:200]
    return out


def show(obj, fields):
    for f in fields:
        val = getattr(obj, f, None)
        if val is not None:
            print(f"    {f}={val}")


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT")
    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())

    print(f"ENDPOINT_HOST={endpoint.split('/')[2]}")
    count = 0
    for agent in client.agents.list():
        count += 1
        print(f"AGENT={getattr(agent, 'name', None)}")
        show(agent, ["id", "kind", "description", "created_at"])
        try:
            for ver in client.agents.list_versions(agent.name):
                print(f"  VERSION={getattr(ver, 'version', None)}")
                show(ver, ["state", "provisioning_state", "created_at", "endpoint"])
                definition = getattr(ver, "definition", None)
                if definition is not None:
                    print(f"    definition_kind={getattr(definition, 'kind', None)}")
                    show(definition, ["cpu", "memory", "protocol_versions"])
                    code = getattr(definition, "code_configuration", None)
                    if code is not None:
                        show(code, ["runtime", "entry_point", "dependency_resolution"])
        except Exception as exc:  # noqa: BLE001
            print(f"  VERSION_LIST_ERROR={type(exc).__name__}: {str(exc)[:200]}")
    print(f"AGENT_COUNT={count}")

    import json

    try:
        vers = sorted((int(v.version) for v in client.agents.list_versions(AGENT_FOCUS)))
        print(f"  VERSIONS_FOUND={vers}")
        detail = client.agents.get_version(AGENT_FOCUS, str(vers[-1]))
        for k, v in sorted(_flatten(detail.as_dict()).items()):
            print(f"  VLATEST.{k}={v}")
    except Exception as exc:  # noqa: BLE001
        print(f"RAW_VERSION_ERROR={type(exc).__name__}: {str(exc)[:300]}")

    try:
        agent = client.agents.get(AGENT_FOCUS)
        for k, v in sorted(_flatten(agent.as_dict()).items()):
            print(f"  A.{k}={v}")
    except Exception as exc:  # noqa: BLE001
        print(f"RAW_AGENT_ERROR={type(exc).__name__}: {str(exc)[:300]}")
    try:
        oai = client.get_openai_client()
        print(f"  OAI_BASE_URL={oai.base_url}")
    except Exception as exc:  # noqa: BLE001
        print(f"  OAI_BASE_URL_ERROR={type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
