"""Requirement 5 - BYOM gateway test against the v2 prompt-agent API.

The legacy /assistants surface rejects connection-qualified models with
`invalid_engine_error`. Per the BYOM documentation, gateway models are only
usable through the v2 prompt-agent API:

    agents.create_version(PromptAgentDefinition(model="<connection>/<model>"))
    -> responses.create(extra_body={"agent_reference": ...})

Runs inside the VNet. Prints RESULT[key]=value lines; values are flattened
because the ACA log pipeline mangles embedded JSON and newlines.
"""
import json
import os
import time
import urllib.request

CONN = os.environ.get("GW_CONNECTION", "poc-llm-gateway")
GW_BASE = os.environ.get("GW_BASE", "").rstrip("/")
ENDPOINT = os.environ["GW_PROJECT_ENDPOINT"].rstrip("/")


def emit(key, value):
    flat = " ".join(str(value).split())
    if len(flat) <= 380:
        print(f"RESULT[{key}]={flat}", flush=True)
        return
    for i in range(0, len(flat), 380):
        print(f"RESULT[{key}#{i // 380}]={flat[i:i + 380]}", flush=True)


def audit(tag):
    """Read the gateway's own request log - the proof traffic transited it."""
    if not GW_BASE:
        return
    try:
        url = GW_BASE.replace("/v1", "") + "/_audit"
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        emit(f"audit.{tag}.count", data.get("count"))
        for i, rec in enumerate(data.get("requests", [])[-4:]):
            emit(f"audit.{tag}.rec{i}",
                 " ".join(f"{k}={v}" for k, v in rec.items()))
    except Exception as exc:  # noqa: BLE001
        emit(f"audit.{tag}.error", f"{type(exc).__name__}: {exc}")


def main():
    from azure.identity import DefaultAzureCredential
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition

    import azure.ai.projects as _p
    emit("sdk.version", getattr(_p, "__version__", "unknown"))
    emit("connection", CONN)

    project = AIProjectClient(endpoint=ENDPOINT, credential=DefaultAzureCredential())

    audit("before")

    envelope_tool = {
        "type": "function",
        "name": "get_envelope_status",
        "description": "Look up a DocuSign envelope status by id.",
        "parameters": {
            "type": "object",
            "properties": {"envelope_id": {"type": "string"}},
            "required": ["envelope_id"],
        },
    }

    for model_name, label, tools in [
        (f"{CONN}/gemini-2.5-pro", "b1_gemini", None),
        (f"{CONN}/gpt-4o-mini", "b2_aoai", None),
        (f"{CONN}/gemini-2.5-pro", "b3_gemini_tools", [envelope_tool]),
    ]:
        agent = None
        try:
            t0 = time.time()
            kwargs = {"model": model_name, "instructions": "Answer briefly."}
            if tools:
                kwargs["tools"] = tools
            agent = project.agents.create_version(
                agent_name=f"gwtest-{label}".replace("_", "-"),
                definition=PromptAgentDefinition(**kwargs),
            )
            emit(f"{label}.created", f"{agent.name} v{agent.version}")
            emit(f"{label}.create_seconds", round(time.time() - t0, 2))

            client = project.get_openai_client()
            t1 = time.time()
            resp = client.responses.create(
                input="Reply with exactly: GATEWAY_OK",
                extra_body={"agent_reference": {"name": agent.name,
                                                "type": "agent_reference"}},
            )
            emit(f"{label}.run_seconds", round(time.time() - t1, 2))
            emit(f"{label}.output", getattr(resp, "output_text", None))
            emit(f"{label}.model_reported", getattr(resp, "model", None))
            # Tool-call evidence: assert on the emitted items, not on prose.
            kinds = [getattr(o, "type", "?") for o in (getattr(resp, "output", None) or [])]
            emit(f"{label}.output_kinds", ",".join(kinds))
            for o in (getattr(resp, "output", None) or []):
                if getattr(o, "type", "") == "function_call":
                    emit(f"{label}.TOOL_CALL",
                         f"{getattr(o, 'name', '?')} args={getattr(o, 'arguments', '?')}")
        except Exception as exc:  # noqa: BLE001
            emit(f"{label}.ERROR_TYPE", type(exc).__name__)
            emit(f"{label}.ERROR", str(exc)[:900])
        finally:
            if agent is not None:
                try:
                    project.agents.delete_version(
                        agent_name=agent.name, agent_version=agent.version)
                    emit(f"{label}.cleaned_up", "yes")
                except Exception as exc:  # noqa: BLE001
                    emit(f"{label}.cleanup_error", str(exc)[:200])

    audit("after")

    # Full body capture of the last exchange - this is what tells us why
    # Foundry accepted or rejected the gateway response.
    try:
        url = GW_BASE.replace("/v1", "") + "/_last"
        with urllib.request.urlopen(url, timeout=30) as resp:
            last = json.loads(resp.read().decode())
        # base64: the ACA log pipeline strips raw JSON braces.
        import base64
        emit("last.b64", base64.b64encode(json.dumps(last).encode()).decode())
    except Exception as exc:  # noqa: BLE001
        emit("last.error", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
