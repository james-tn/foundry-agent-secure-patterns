"""LangGraph hosted agent that probes code execution and state persistence.

Track D / D3 + D4. This is the hosted-agent counterpart to Track C (code
execution) and to the Foundry-managed thread store (state).

The point of this agent is to establish, by measurement rather than by reading
docs, two things that differ sharply from the prompt-agent model:

D3 - code execution. A prompt agent has no process of its own, so running code
means calling out to Code Interpreter or to an Azure Container Apps dynamic
session. A hosted agent already *is* a process, so it can simply exec. The
`run_python` tool below does exactly that and then reports what that process
could see - environment variables, the managed identity, the network. That
report is the finding: in-process execution is fast but shares the agent's
whole security context, so it is not a sandbox.

D4 - state. Prompt agents get a Foundry-managed thread store (optionally backed
by a customer Cosmos account). A hosted agent owns its own persistence. The
`state_write` / `state_read` tools below talk to a private-endpoint Cosmos
account using the agent's own managed identity, proving the hosted agent can
reach customer data stores over the private network with no keys.
"""
from __future__ import annotations

import io
import json
import os
import socket
import time
import traceback
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from typing import Annotated

from azure.ai.projects import AIProjectClient
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from langchain_azure_ai.agents.hosting import ResponsesHostServer

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT", "").rstrip("/")
COSMOS_DATABASE = os.environ.get("COSMOS_DATABASE", "trackd_state")
COSMOS_CONTAINER = os.environ.get("COSMOS_CONTAINER", "checkpoints")

_credential = DefaultAzureCredential()
_cosmos_container = None


def _container():
    global _cosmos_container
    if _cosmos_container is None:
        client = CosmosClient(COSMOS_ENDPOINT, credential=_credential)
        _cosmos_container = client.get_database_client(COSMOS_DATABASE).get_container_client(
            COSMOS_CONTAINER
        )
    return _cosmos_container


@tool
def run_python(
    code: Annotated[str, "Python source to execute. Use print() to produce output."],
) -> str:
    """Execute Python code and return whatever it prints, plus timing."""
    started = time.perf_counter()
    buffer = io.StringIO()
    result = {"execution_mode": "in_process_hosted_container"}
    try:
        with redirect_stdout(buffer):
            exec(compile(code, "<agent_tool>", "exec"), {"__name__": "__main__"})  # noqa: S102
        result["stdout"] = buffer.getvalue()[:4000]
    except Exception:  # noqa: BLE001
        result["stdout"] = buffer.getvalue()[:2000]
        result["traceback"] = traceback.format_exc()[-800:]
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return json.dumps(result)


@tool
def describe_execution_context() -> str:
    """Report what the code-execution environment can see: identity, secrets, network."""
    report: dict[str, object] = {"hostname": socket.gethostname(), "pid": os.getpid()}

    interesting = [k for k in os.environ if not k.startswith(("LC_", "LANG"))]
    report["env_var_count"] = len(interesting)
    report["env_vars_visible_to_executed_code"] = sorted(interesting)[:40]

    try:
        token = _credential.get_token(_AZURE_AI_SCOPE)
        report["managed_identity_reachable_from_exec"] = bool(token.token)
    except Exception as exc:  # noqa: BLE001
        report["managed_identity_reachable_from_exec"] = f"error:{type(exc).__name__}"

    egress = {}
    for label, url in (
        ("public_internet", "https://example.com"),
        ("azure_control_plane", "https://management.azure.com/"),
    ):
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                egress[label] = f"reachable:{resp.status}"
        except urllib.error.HTTPError as exc:
            egress[label] = f"reachable:{exc.code}"
        except Exception as exc:  # noqa: BLE001
            egress[label] = f"blocked:{type(exc).__name__}"
        egress[f"{label}_ms"] = round((time.perf_counter() - started) * 1000, 1)
    report["egress"] = egress
    return json.dumps(report)


@tool
def state_write(
    thread_id: Annotated[str, "Conversation or thread identifier."],
    note: Annotated[str, "Text to persist for this thread."],
) -> str:
    """Persist a note for a thread into the private Cosmos state store."""
    started = time.perf_counter()
    item = {
        "id": f"{thread_id}-{int(time.time() * 1000)}",
        "thread_id": thread_id,
        "note": note,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _container().create_item(item)
        return json.dumps(
            {
                "persisted": True,
                "id": item["id"],
                "auth": "managed_identity_aad",
                "endpoint_host": COSMOS_ENDPOINT.split("//")[-1],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    except cosmos_exceptions.CosmosHttpResponseError as exc:
        return json.dumps({"persisted": False, "error": f"CosmosHttpResponseError:{exc.status_code}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"persisted": False, "error": f"{type(exc).__name__}:{str(exc)[:200]}"})


@tool
def state_read(
    thread_id: Annotated[str, "Conversation or thread identifier to read back."],
) -> str:
    """Read back every note previously persisted for a thread."""
    started = time.perf_counter()
    try:
        items = list(
            _container().query_items(
                query="SELECT c.note, c.written_at FROM c WHERE c.thread_id=@t",
                parameters=[{"name": "@t", "value": thread_id}],
                partition_key=thread_id,
            )
        )
        return json.dumps(
            {
                "count": len(items),
                "items": items[:20],
                "auth": "managed_identity_aad",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{type(exc).__name__}:{str(exc)[:200]}"})


def _build_chat_model() -> ChatOpenAI:
    deployment = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4o-mini")
    project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_credential)
    openai_client = project.get_openai_client()
    token_provider = get_bearer_token_provider(_credential, _AZURE_AI_SCOPE)
    return ChatOpenAI(
        model=deployment,
        base_url=str(openai_client.base_url),
        api_key=token_provider,
        use_responses_api=True,
        output_version="responses/v1",
    )


def main() -> None:
    graph = create_agent(
        _build_chat_model(),
        tools=[run_python, describe_execution_context, state_write, state_read],
    )
    ResponsesHostServer(graph).run(port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
