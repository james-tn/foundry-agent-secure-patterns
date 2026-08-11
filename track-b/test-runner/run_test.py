import json
import os
import sys
import urllib.error
import urllib.request

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    OpenApiFunctionDefinition,
    OpenApiProjectConnectionAuthDetails,
    OpenApiProjectConnectionSecurityScheme,
    OpenApiTool,
    PromptAgentDefinition,
)
from azure.identity import DefaultAzureCredential


project_endpoint = os.environ["PROJECT_ENDPOINT"]
model_name = os.environ.get("MODEL_NAME", "gpt-4o-mini")
api_url = os.environ["OPENAPI_SERVER_PRIVATE"].rstrip("/")
connection_id = os.environ["OPENAPI_CONNECTION_ID"]


def load_openapi_spec() -> dict:
    with urllib.request.urlopen(f"{api_url}/openapi.json", timeout=30) as response:
        spec = json.load(response)
    spec["servers"] = [{"url": api_url, "description": "Private internal API"}]
    return spec


def main() -> int:
    print(f"PRIVATE_API_URL={api_url}", flush=True)
    with urllib.request.urlopen(f"{api_url}/healthz", timeout=30) as response:
        print(f"PRIVATE_API_HEALTH={response.read().decode()}", flush=True)

    unauthenticated_request = urllib.request.Request(
        f"{api_url}/internal/envelopes/env-1001"
    )
    try:
        urllib.request.urlopen(unauthenticated_request, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        print("UNAUTHENTICATED_REQUEST=BLOCKED_401", flush=True)
    else:
        print("RESULT=FAILED: private API accepted a request without credentials", flush=True)
        return 1

    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=project_endpoint, credential=credential)
    openai_client = project_client.get_openai_client()

    auth = OpenApiProjectConnectionAuthDetails(
        security_scheme=OpenApiProjectConnectionSecurityScheme(
            project_connection_id=connection_id
        )
    )
    tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name="internal_api",
            description="Read envelope status from the private internal API.",
            spec=load_openapi_spec(),
            auth=auth,
        )
    )

    agent = project_client.agents.create_version(
        agent_name="private-internal-api-poc",
        definition=PromptAgentDefinition(
            model=model_name,
            instructions=(
                "You can retrieve envelope status only through the internal API tool. "
                "Always call the tool. Return the envelope ID, status, and document name."
            ),
            tools=[tool],
        ),
    )
    print(f"AGENT_VERSION={agent.name}:{agent.version}", flush=True)

    conversation = openai_client.conversations.create()
    response = openai_client.responses.create(
        conversation=conversation.id,
        input="Use the internal API to get the current status of envelope env-1001.",
        extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
    )
    print(f"AGENT_RESPONSE={response.output_text}", flush=True)

    normalized = response.output_text.lower()
    if "env-1001" not in normalized or "completed" not in normalized:
        print("RESULT=FAILED: expected envelope ID and completed status", flush=True)
        return 1

    print("RESULT=PASSED: private OpenAPI tool returned the internal envelope status", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
