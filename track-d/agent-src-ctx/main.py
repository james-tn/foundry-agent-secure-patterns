"""LangGraph hosted agent that echoes the request context it received.

Track D / Requirement 4. The question is whether customer-defined request
context - end-user identity, tenant id, correlation ids - can be carried from
the caller, through the Foundry ingress, into agent code, and onward to
downstream tools and APIs.

The AgentServer SDK defines a wire contract for this (see
``azure.ai.agentserver.core.platform_headers``):

* ``x-client-*``            - arbitrary caller headers, passed through to the
                              handler as ``ResponseContext.client_headers``
* ``x-agent-user-id``       - platform-injected, cross-agent per-user identity
* ``x-agent-foundry-call-id`` - opaque per-request call id that the container
                              must forward on outbound calls to Foundry
                              services so they can resolve caller context
* ``traceparent``           - W3C trace context

Reading the SDK only proves the container *would* surface these. It does not
prove the Foundry ingress forwards them. This agent measures what actually
arrives, by capturing the context on the way in and exposing it through a tool.
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from langchain_azure_ai.agents.hosting import ResponsesHostServer

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"
PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")

_credential = DefaultAzureCredential()

# Captured on the way in, read by the tool during the same request.
_REQUEST_CONTEXT: ContextVar[dict] = ContextVar("request_context")
_LAST_CONTEXT: dict = {}


def _capture(request, context) -> dict:
    """Pull everything the platform makes available about this request."""
    captured: dict = {}

    def safe(label, fn):
        try:
            captured[label] = fn()
        except Exception as exc:  # noqa: BLE001
            captured[label] = f"error:{type(exc).__name__}"

    safe("client_headers", lambda: dict(getattr(context, "client_headers", {}) or {}))
    safe("query_parameters", lambda: dict(getattr(context, "query_parameters", {}) or {}))
    safe("session_id", lambda: getattr(context, "session_id", None))
    safe("conversation_id", lambda: getattr(context, "conversation_id", None))
    safe("request_metadata", lambda: dict(getattr(request, "metadata", {}) or {}))

    platform = getattr(context, "platform_context", None)
    safe("platform_user_id", lambda: getattr(platform, "user_id_key", None))
    safe("platform_call_id", lambda: getattr(platform, "call_id", None))

    def _sdk_context():
        from azure.ai.agentserver.core import get_request_context

        ctx = get_request_context()
        return {"call_id": ctx.call_id, "user_id": ctx.user_id, "session_id": ctx.session_id}

    safe("sdk_request_context", _sdk_context)

    def _trace():
        from opentelemetry import trace

        span = trace.get_current_span().get_span_context()
        if not span.is_valid:
            return None
        return {"trace_id": format(span.trace_id, "032x"), "span_id": format(span.span_id, "016x")}

    safe("otel_span", _trace)
    return captured


@tool
def echo_request_context() -> str:
    """Report the caller-supplied request context this agent received."""
    return json.dumps(_REQUEST_CONTEXT.get(_LAST_CONTEXT))


@tool
def call_downstream_with_context() -> str:
    """Show exactly which headers this agent would forward to a downstream API."""
    captured = _REQUEST_CONTEXT.get(_LAST_CONTEXT)
    forwarded = dict((captured.get("client_headers") or {}))
    try:
        from azure.ai.agentserver.core import get_request_context

        forwarded.update(get_request_context().platform_headers())
    except Exception as exc:  # noqa: BLE001
        forwarded["_platform_headers_error"] = type(exc).__name__
    return json.dumps({"would_forward": forwarded})


class ContextCapturingHost(ResponsesHostServer):
    """Captures platform request context before the graph runs."""

    async def build_runnable_config(self, request, context):
        config = await super().build_runnable_config(request, context)
        captured = _capture(request, context)
        _REQUEST_CONTEXT.set(captured)
        global _LAST_CONTEXT
        _LAST_CONTEXT = captured
        configurable = config.setdefault("configurable", {})
        configurable["request_context"] = captured
        return config


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
        _build_chat_model(), tools=[echo_request_context, call_downstream_with_context]
    )
    ContextCapturingHost(graph).run(port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
