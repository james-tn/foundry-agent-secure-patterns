"""LangGraph hosted agent that calls a private, VNet-only internal API.

Track D / D1. This is the hosted-agent counterpart to the Track B prompt agent.

The difference that matters for a security review:

* Prompt agent - the Foundry data proxy calls the API and injects the key from
  a project connection. The agent never handles the credential.
* Hosted agent (this file) - the agent's own code makes the call from its own
  NIC in the delegated subnet, so it must obtain the credential itself. It does
  that with its managed identity against the project connection, so the key is
  still never baked into the image or the environment.

The tool reports how it resolved the credential so the demo can prove which
path was used.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Annotated

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from langchain_azure_ai.agents.hosting import ResponsesHostServer

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"

INTERNAL_API_BASE = os.environ["INTERNAL_API_BASE"].rstrip("/")
CONNECTION_NAME = os.environ.get("INTERNAL_API_CONNECTION", "")
PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")

_credential = DefaultAzureCredential()


def _resolve_api_key() -> tuple[str | None, str]:
    """Return (key, how_it_was_resolved)."""
    if CONNECTION_NAME:
        try:
            project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_credential)
            conn = project.connections.get(CONNECTION_NAME, include_credentials=True)
            creds = getattr(conn, "credentials", None)
            for attr in ("api_key", "key"):
                value = getattr(creds, attr, None)
                if value:
                    return value, "project_connection"
            as_dict = conn.as_dict() if hasattr(conn, "as_dict") else {}
            bag = as_dict.get("credentials") or {}
            for candidate in bag.values():
                if isinstance(candidate, str) and candidate:
                    return candidate, "project_connection"
            return None, f"project_connection_no_secret:{list(bag)}"
        except Exception as exc:  # noqa: BLE001
            return None, f"project_connection_error:{type(exc).__name__}"
    return None, "not_attempted"


@tool
def get_envelope_status(
    envelope_id: Annotated[str, "The internal envelope id, for example env-1001."],
) -> str:
    """Look up the status of an internal envelope from the private internal API."""
    api_key, how = _resolve_api_key()
    url = f"{INTERNAL_API_BASE}/internal/envelopes/{envelope_id}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Internal-Api-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            body["_credential_source"] = how
            return json.dumps(body)
    except urllib.error.HTTPError as exc:
        return json.dumps(
            {"error": f"HTTP {exc.code}", "detail": exc.read().decode()[:200], "_credential_source": how}
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": type(exc).__name__, "detail": str(exc)[:200], "_credential_source": how})


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
    graph = create_agent(_build_chat_model(), tools=[get_envelope_status])
    ResponsesHostServer(graph).run(port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
