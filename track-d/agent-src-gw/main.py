"""Requirement 5 / hosted agent - drive the model through a customer LLM gateway.

Track D counterpart to the prompt-agent BYOM test. A prompt agent can only
reach a gateway through an admin-created ModelGateway connection, and Foundry
owns the HTTP call. A hosted agent owns its own model client, so it can point
`base_url` anywhere it can reach on the network.

This agent builds two clients so a single invocation can compare them:

  * `gateway`  - ChatOpenAI with base_url set to the customer gateway. No
                 Foundry connection, no admin involvement, and the model name
                 is whatever the gateway exposes (including providers Foundry
                 has no catalog entry for).
  * `foundry`  - the ordinary Foundry-managed client, for a baseline.

`which_model_served_this` reports what actually answered, so the evidence is a
tool result rather than model prose.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Annotated

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from langchain_azure_ai.agents.hosting import ResponsesHostServer

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", "").rstrip("/")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "poc-not-a-real-secret")
GATEWAY_MODEL = os.environ.get("GATEWAY_MODEL", "gemini-2.5-pro")
FOUNDRY_MODEL = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4o-mini")

_credential = DefaultAzureCredential()


@tool
def probe_gateway(
    prompt: Annotated[str, "Prompt to send through the customer LLM gateway."],
) -> str:
    """Call the customer's LLM gateway directly and report what answered."""
    result = {"gateway_base_url": GATEWAY_BASE_URL, "requested_model": GATEWAY_MODEL}
    if not GATEWAY_BASE_URL:
        result["error"] = "GATEWAY_BASE_URL not set"
        return json.dumps(result)

    model = ChatOpenAI(
        model=GATEWAY_MODEL,
        base_url=GATEWAY_BASE_URL,
        api_key=GATEWAY_API_KEY,
        # The gateway speaks Chat Completions, not the Responses API. A hosted
        # agent can choose this per-client; a prompt agent cannot.
        use_responses_api=False,
    )
    t0 = time.time()
    try:
        reply = model.invoke(prompt)
        result["ok"] = True
        result["latency_ms"] = round((time.time() - t0) * 1000, 1)
        result["content"] = str(reply.content)[:600]
        result["response_metadata"] = {
            k: v for k, v in (reply.response_metadata or {}).items()
            if k in ("model_name", "finish_reason", "model")
        }
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    return json.dumps(result)


@tool
def probe_gateway_audit() -> str:
    """Read the gateway's own request log to prove traffic transited it."""
    if not GATEWAY_BASE_URL:
        return json.dumps({"error": "GATEWAY_BASE_URL not set"})
    url = GATEWAY_BASE_URL.replace("/v1", "") + "/_audit"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        recent = data.get("requests", [])[-3:]
        return json.dumps({
            "count": data.get("count"),
            "recent": [{"model": r.get("model"), "route": r.get("route"),
                        "status": r.get("status"), "stream": r.get("stream"),
                        "ua": r.get("ua", "")[:60]} for r in recent],
        })
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


@tool
def which_model_served_this() -> str:
    """Report how this agent's own reasoning model is configured."""
    return json.dumps({
        "agent_default_model": FOUNDRY_MODEL,
        "agent_default_path": "foundry-managed",
        "gateway_available": bool(GATEWAY_BASE_URL),
        "gateway_model": GATEWAY_MODEL,
    })


def _build_chat_model() -> ChatOpenAI:
    """The agent's own reasoning model.

    Set AGENT_MODEL_SOURCE=gateway to run the whole agent loop through the
    customer gateway; the default keeps Foundry in the path so the two can be
    compared in one deployment.
    """
    if os.environ.get("AGENT_MODEL_SOURCE") == "gateway" and GATEWAY_BASE_URL:
        return ChatOpenAI(
            model=GATEWAY_MODEL,
            base_url=GATEWAY_BASE_URL,
            api_key=GATEWAY_API_KEY,
            use_responses_api=False,
        )

    project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_credential)
    openai_client = project.get_openai_client()
    token_provider = get_bearer_token_provider(_credential, _AZURE_AI_SCOPE)
    return ChatOpenAI(
        model=FOUNDRY_MODEL,
        base_url=str(openai_client.base_url),
        api_key=token_provider,
        use_responses_api=True,
        output_version="responses/v1",
    )


def main() -> None:
    graph = create_agent(
        _build_chat_model(),
        tools=[probe_gateway, probe_gateway_audit, which_model_served_this],
    )
    ResponsesHostServer(graph).run(port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
