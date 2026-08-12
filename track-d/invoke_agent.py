"""Track D - invoke the hosted LangGraph agent and time it.

Hosted agents are not addressable as models on the project Responses endpoint.
The service routes them through a per-agent endpoint:

    {project}/agents/{name}/endpoint/protocols/openai/responses

Runs in-VNet. Emits timing so the same script backs the cold-start measurement.
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "probes"))
import _config  # noqa: E402

from azure.identity import DefaultAzureCredential  # noqa: E402

AGENT = os.environ.get("TRACKD_AGENT_NAME", "trackd-langgraph")
PROMPT = os.environ.get("TRACKD_PROMPT", "What is 6817 multiplied by 41? Use the calculator tool.")
LABEL = os.environ.get("TRACKD_LABEL", "invoke")


API_VERSIONS = os.environ.get(
    "TRACKD_API_VERSIONS",
    "2025-11-15-preview",
).split(",")


def agent_url(endpoint: str, agent: str, api_version: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/agents/{agent}/endpoint/protocols/openai/responses"
        f"?api-version={api_version}"
    )


def invoke(url: str, token: str, prompt: str, timeout: int = 600):
    body = json.dumps({"input": prompt}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
            return resp.status, payload, time.time() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:500], time.time() - started
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}", time.time() - started


def extract_text(payload) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:300]
    chunks = []
    for item in payload.get("output", []):
        for content in item.get("content", []) or []:
            if content.get("type") in ("output_text", "text"):
                chunks.append(content.get("text", ""))
    return " ".join(chunks)[:400]


def non_message_output(payload) -> list:
    if not isinstance(payload, dict):
        return []
    return [i.get("type") for i in payload.get("output", []) if i.get("type") != "message"]


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT")
    token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token
    print(f"LABEL={LABEL}")
    print(f"AGENT={AGENT}")
    print(f"URL_PATH=/agents/{AGENT}/endpoint/protocols/openai/responses")

    api_version = API_VERSIONS[0].strip()
    url = agent_url(endpoint, AGENT, api_version)
    print(f"API_VERSION={api_version}")

    deadline = time.time() + int(os.environ.get("TRACKD_READY_TIMEOUT", "900"))
    wall_start = time.time()
    attempts = 0
    first_424_logged = False
    while True:
        attempts += 1
        status, payload, elapsed = invoke(url, token, PROMPT)
        if status == 424:
            if not first_424_logged:
                detail = str(payload).replace('"', "'").replace(chr(10), " ")
                print(f"  first_424={detail[:300]}")
                first_424_logged = True
            if attempts % 6 == 0:
                print(f"  waiting... attempts={attempts} elapsed_s={time.time()-wall_start:.0f}")
            if time.time() > deadline:
                print(f"RESULT=SESSION_NEVER_READY attempts={attempts}")
                return 1
            time.sleep(5)
            continue
        break

    total = time.time() - wall_start
    print(f"ATTEMPTS={attempts}")
    print(f"TIME_TO_FIRST_RESPONSE_S={total:.2f}")
    print(f"STATUS={status}")
    print(f"ELAPSED_S={elapsed:.2f}")
    if status != 200:
        detail = str(payload).replace('"', "'").replace("\n", " ")
        print(f"RESULT=INVOKE_FAILED DETAIL={detail[:400]}")
        return 1

    print(f"NON_MESSAGE_OUTPUT={non_message_output(payload)}")
    for item in payload.get("output", []):
        if item.get("type") == "function_call_output":
            # ACA truncates long log lines, so emit the payload in numbered
            # chunks and reassemble on the caller's side.
            raw = str(item.get("output")).replace('"', "'").replace(chr(10), " ")
            chunks = [raw[i : i + 400] for i in range(0, min(len(raw), 8000), 400)] or [""]
            for idx, chunk in enumerate(chunks):
                print(f"TOOL_OUTPUT[{idx}]={chunk}")
        if item.get("type") == "function_call":
            print(f"TOOL_CALL name={item.get('name')} args={str(item.get('arguments'))[:120]}")
    print(f"OUTPUT_TEXT={extract_text(payload).replace(chr(10), ' ')}")
    print("RESULT=INVOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
