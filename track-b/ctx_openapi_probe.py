"""Requirement 4 - what request context can a PROMPT agent propagate downstream?

The hosted-agent answer is measured in track-d (x-client-* headers, metadata and
traceparent all reach agent code). Prompt agents have no equivalent documented
channel, and "no documented mechanism" is a claim worth testing rather than
repeating, because the alternative for the customer is a redesign.

Method: point a prompt agent at an OpenAPI tool served by context-echo, invoke it
with caller-supplied headers, metadata and a traceparent, then read what the API
actually received. The API records the wire, so the model cannot flatter us.

Three variants are run:
  1. baseline        - nothing special, see what the platform injects
  2. caller-context  - x-client-* headers + metadata + traceparent on the call
  3. model-mediated  - instructions tell the model to pass a correlation id as a
                       tool *parameter*, the only channel it demonstrably has

Runs in-VNet: the project data plane and the echo API are both private.
"""
import json
import os
import pathlib
import sys
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "probes"))
try:
    import _config  # noqa: F401
except Exception:  # noqa: BLE001
    pass

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.ai.projects.models import (  # noqa: E402
    OpenApiAnonymousAuthDetails,
    OpenApiFunctionDefinition,
    OpenApiTool,
    PromptAgentDefinition,
)
from azure.identity import DefaultAzureCredential  # noqa: E402

PROJECT_ENDPOINT = os.environ["CTX_PROJECT_ENDPOINT"]
MODEL = os.environ.get("CTX_MODEL", "gpt-4o-mini")
ECHO = os.environ["CTX_ECHO_URL"].rstrip("/")
AGENT_NAME = os.environ.get("CTX_AGENT_NAME", "ctx-openapi-probe")

TRACEPARENT = "00-11112222333344445555666677778888-9999aaaabbbbcccc-01"
CORRELATION = "corr-prompt-77"

# CTX_BAGGAGE_PAD sizes a filler entry so the practical ceiling can be found;
# the W3C spec allows 8192 bytes but says nothing about what a proxy enforces.
_PAD = int(os.environ.get("CTX_BAGGAGE_PAD", "0"))
BAGGAGE = f"docusign_tenant=contoso-eu,docusign_corr={CORRELATION}"
if _PAD:
    BAGGAGE += ",docusign_pad=" + ("x" * _PAD)


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{ECHO}{path}", timeout=60) as resp:
        return json.loads(resp.read().decode())


def emit_received(label: str) -> None:
    data = get_json("/_last")
    print(f"F {label}.BAGGAGE_SENT_BYTES={len(BAGGAGE)}")
    print(f"F {label}.TOOL_CALLS_RECEIVED={data['count']}")
    for entry in data["recent"]:
        headers = entry["headers"]
        print(f"F {label}.QUERY={entry['query']}")
        print(f"F {label}.HEADER_NAMES={sorted(headers)}")
        for key in sorted(headers):
            if key in ("host", "content-length", "accept-encoding", "connection"):
                continue
            value = headers[key]
            if key == "baggage":
                print(f"F {label}.BAGGAGE_RECV_BYTES={len(value)}")
                value = value.replace("x" * 40, "x{40}...")
            print(f"F {label}.HDR {key}={value[:200]}")


def load_spec() -> dict:
    spec = get_json("/openapi.json")
    spec["servers"] = [{"url": ECHO}]
    return spec


def main() -> int:
    print(f"F ECHO_HOST={ECHO.split('//')[-1].split('.')[0]}")
    print(f"F HEALTH={get_json('/healthz')}")

    credential = DefaultAzureCredential()
    project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential)
    openai_client = project.get_openai_client()

    tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name="internal_api",
            description="Read envelope status from the internal API.",
            spec=load_spec(),
            auth=OpenApiAnonymousAuthDetails(),
        )
    )

    instructions = (
        "Retrieve envelope status only through the internal API tool. "
        "Always call the tool. Report the envelope id and status."
    )
    if os.environ.get("CTX_MODEL_MEDIATED") == "1":
        instructions += (
            f" When you call the tool you MUST pass correlation_id='{CORRELATION}'."
        )

    agent = project.agents.create_version(
        agent_name=AGENT_NAME,
        definition=PromptAgentDefinition(
            model=MODEL, instructions=instructions, tools=[tool]
        ),
    )
    print(f"F AGENT_VERSION={agent.name}:{agent.version}")

    get_json("/_reset")

    # Caller-supplied context, exactly as the hosted-agent probe sends it.
    extra_headers = {
        "x-client-tenant-id": "contoso-eu",
        "x-client-correlation-id": CORRELATION,
        "x-client-end-user": "alex@example.internal",
        "x-custom-not-prefixed": "should-be-dropped",
        "traceparent": TRACEPARENT,
        # W3C baggage is the standard carrier for custom key/value context, and
        # the platform was observed setting it on outbound tool calls. If a
        # caller-supplied value survives, prompt agents have a context channel.
        "baggage": BAGGAGE,
    }

    conversation = openai_client.conversations.create()
    response = openai_client.responses.create(
        conversation=conversation.id,
        input="Use the internal API to get the status of envelope env-1001.",
        extra_body={
            "agent_reference": {"name": agent.name, "type": "agent_reference"},
            "metadata": {"tenant": "contoso-eu", "request_id": "req-999"},
        },
        extra_headers=extra_headers,
    )
    print(f"F OUTPUT_TEXT={response.output_text[:200]}")

    label = "MODELMED" if os.environ.get("CTX_MODEL_MEDIATED") == "1" else "CALLERCTX"
    emit_received(label)
    print("F RESULT=CTX_PROBE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
