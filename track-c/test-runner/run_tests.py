import json
import os
import statistics
import sys
import time
import urllib.request
import uuid

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential


project_endpoint = os.environ["PROJECT_ENDPOINT"]
model_name = os.environ.get("MODEL_NAME", "gpt-4o-mini")
mcp_connection_id = os.environ["MCP_CONNECTION_ID"]
custom_pool_endpoint = os.environ["CUSTOM_POOL_ENDPOINT"].rstrip("/")

# Documented data-plane version and route (`/executions`, top-level body).
# The older `/execute` + {"code": ...} form still works but is undocumented.
SESSIONS_DATA_PLANE_API_VERSION = os.environ.get(
    "SESSIONS_API_VERSION", "2025-10-02-preview"
)
BUILTIN_SAMPLES = int(os.environ.get("BUILTIN_SAMPLES", "3"))


def timed_response(client, **kwargs):
    started = time.perf_counter()
    response = client.responses.create(**kwargs)
    return time.perf_counter() - started, response


def run_agent_test(openai_client, agent_name, prompt, expected, attempts=3):
    last_response = None
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            elapsed, response = timed_response(
                openai_client,
                input=prompt,
                extra_body={
                    "agent_reference": {"name": agent_name, "type": "agent_reference"}
                },
            )
        except Exception as error:
            last_error = error
            print(
                f"CUSTOM_ATTEMPT={attempt} ERROR={type(error).__name__}: {error}",
                flush=True,
            )
            continue
        last_response = response
        text = response.output_text
        print(
            f"CUSTOM_ATTEMPT={attempt} ELAPSED={elapsed:.2f}s OUTPUT={text}",
            flush=True,
        )
        if expected.lower() in text.lower():
            return elapsed, text
    if last_response is not None:
        raise RuntimeError(
            f"Expected {expected!r} after {attempts} attempts; "
            f"last output was {last_response.output_text!r}"
        )
    raise RuntimeError(f"All {attempts} attempts failed; last error: {last_error}")


def run_custom_code(credential, session_id, name, code):
    token = credential.get_token("https://dynamicsessions.io/.default").token
    payload = {
        "codeInputType": "Inline",
        "executionType": "Synchronous",
        "code": code,
        "timeoutInSeconds": 60,
    }
    request = urllib.request.Request(
        (
            f"{custom_pool_endpoint}/executions"
            f"?api-version={SESSIONS_DATA_PLANE_API_VERSION}&identifier={session_id}"
        ),
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    elapsed = time.perf_counter() - started
    execution = result.get("result", {})
    stdout = execution.get("stdout", "").strip()
    print(
        f"CUSTOM_{name}_ELAPSED={elapsed:.3f}s "
        f"STATUS={result.get('status')} "
        f"EXEC_MS={execution.get('executionTimeInMilliseconds')} "
        f"OUTPUT={stdout}",
        flush=True,
    )
    return elapsed, stdout


def main() -> int:
    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=project_endpoint, credential=credential)
    openai_client = project_client.get_openai_client(max_retries=0, timeout=90)

    custom_session_id = f"trackc-{uuid.uuid4().hex[:12]}"
    custom_packages_elapsed, custom_packages = run_custom_code(
        credential,
        custom_session_id,
        "PACKAGES",
        (
            "import polars, pyarrow, matplotlib; "
            "df=polars.DataFrame({'x':[1,2,3]}); "
            "print(f'PACKAGES_OK polars={polars.__version__} "
            "pyarrow={pyarrow.__version__} matplotlib={matplotlib.__version__} "
            "rows={df.height}')"
        ),
    )
    if "PACKAGES_OK" not in custom_packages:
        raise RuntimeError("Approved packages were not available in the custom image")

    custom_egress_elapsed, custom_egress = run_custom_code(
        credential,
        custom_session_id,
        "EGRESS",
        (
            "import urllib.request\n"
            "try:\n"
            " urllib.request.urlopen('https://example.com',timeout=5)\n"
            " print('EGRESS_ALLOWED')\n"
            "except Exception as e:\n"
            " print('EGRESS_BLOCKED',type(e).__name__)"
        ),
    )
    if "EGRESS_BLOCKED" not in custom_egress or "EGRESS_ALLOWED" in custom_egress:
        raise RuntimeError("Custom interpreter unexpectedly had internet egress")

    custom_file_elapsed, custom_file = run_custom_code(
        credential,
        custom_session_id,
        "FILE",
        (
            "import base64; "
            "data=b'envelope_id,status\\nenv-1001,completed\\n'; "
            "print('FILE_DATA_URI_OK data:text/csv;base64,'"
            "+base64.b64encode(data).decode())"
        ),
    )
    if "FILE_DATA_URI_OK data:text/csv;base64," not in custom_file:
        raise RuntimeError("Custom interpreter did not return the file data URI")

    builtin_prompt = (
        "Using the python tool, compute the SHA-256 hex digest of the exact "
        "string 'trackc-{n}' and the {k}th prime number. You MUST run code; "
        "do not answer from memory. Return only: <digest> <prime>"
    )
    builtin_samples = []
    for sample in range(BUILTIN_SAMPLES):
        builtin_started = time.perf_counter()
        try:
            builtin_response = openai_client.responses.create(
                model=model_name,
                tools=[{"type": "code_interpreter", "container": {"type": "auto"}}],
                input=builtin_prompt.format(n=sample, k=1500 + sample),
            )
            builtin_elapsed = time.perf_counter() - builtin_started
            builtin_tool_called = any(
                getattr(item, "type", "") == "code_interpreter_call"
                for item in builtin_response.output
            )
            builtin_status = "TOOL_CALLED" if builtin_tool_called else "NO_TOOL_CALL"
            if builtin_tool_called:
                builtin_samples.append(builtin_elapsed)
            print(
                f"BUILTIN[{sample}]_ELAPSED={builtin_elapsed:.2f}s "
                f"BUILTIN_STATUS={builtin_status} "
                f"OUTPUT={builtin_response.output_text[:80]!r}",
                flush=True,
            )
        except Exception as error:
            builtin_elapsed = time.perf_counter() - builtin_started
            builtin_status = f"ERROR_{type(error).__name__}"
            print(
                f"BUILTIN[{sample}]_ELAPSED={builtin_elapsed:.2f}s "
                f"BUILTIN_STATUS={builtin_status} ERROR={str(error)[:160]}",
                flush=True,
            )

    if builtin_samples:
        builtin_elapsed = statistics.median(builtin_samples)
        builtin_status = f"TOOL_CALLED_{len(builtin_samples)}/{BUILTIN_SAMPLES}"
        print(
            f"BUILTIN_SUMMARY n={len(builtin_samples)} "
            f"median={builtin_elapsed:.2f}s "
            f"min={min(builtin_samples):.2f}s max={max(builtin_samples):.2f}s",
            flush=True,
        )
    else:
        builtin_elapsed = float("nan")
        builtin_status = f"NO_SUCCESSFUL_TOOL_CALL_0/{BUILTIN_SAMPLES}"
        print(f"BUILTIN_SUMMARY {builtin_status}", flush=True)

    mcp_tool = MCPTool(
        server_url="https://localhost",
        server_label="python_sessions",
        require_approval="never",
        allowed_tools=["launchShell", "runPythonCodeInRemoteEnvironment"],
        project_connection_id=mcp_connection_id,
    )
    agent = project_client.agents.create_version(
        agent_name="controlled-code-poc",
        definition=PromptAgentDefinition(
            model=model_name,
            temperature=0,
            instructions=(
                "You execute code only through the python_sessions MCP server. "
                "Always call launchShell before runPythonCodeInRemoteEnvironment. "
                "Use the exact session identifier trackc-session for every tool call; "
                "session identifiers shorter than four characters are invalid. "
                "Never infer execution results. Return the exact marker printed by the code. "
                "For file output, print a data URI rather than a remote path."
            ),
            tools=[mcp_tool],
        ),
    )
    print(f"CUSTOM_AGENT={agent.name}:{agent.version}", flush=True)

    mcp_execution_elapsed, _ = run_agent_test(
        openai_client,
        agent.name,
        (
            "Run Python that calculates sum(range(100)) and prints exactly "
            "MCP_EXECUTION_OK result=<result>."
        ),
        "MCP_EXECUTION_OK result=4950",
    )

    mcp_egress_elapsed, egress_text = run_agent_test(
        openai_client,
        agent.name,
        (
            "Run Python that attempts urllib.request.urlopen('https://example.com', "
            "timeout=5). If it raises any exception print exactly MCP_EGRESS_BLOCKED "
            "and the exception type. If it succeeds print exactly MCP_EGRESS_ALLOWED."
        ),
        "MCP_EGRESS_BLOCKED",
    )
    if "MCP_EGRESS_ALLOWED" in egress_text:
        raise RuntimeError("PythonLTS MCP interpreter unexpectedly had internet egress")

    print(
        "RESULT=PASSED "
        f"custom_packages={custom_packages_elapsed:.2f}s "
        f"custom_egress={custom_egress_elapsed:.2f}s "
        f"custom_file={custom_file_elapsed:.2f}s "
        f"builtin={builtin_elapsed:.2f}s({builtin_status}) "
        f"mcp_execution={mcp_execution_elapsed:.2f}s "
        f"mcp_egress={mcp_egress_elapsed:.2f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"RESULT=FAILED {type(error).__name__}: {error}", flush=True)
        raise
