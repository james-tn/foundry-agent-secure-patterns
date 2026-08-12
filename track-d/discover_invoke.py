"""Track D - discover the hosted-agent invocation route.

The service does not publish the agent URL in the agent record, so this probes
the candidate shapes and reports which the service accepts. Runs in-VNet.
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

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402

AGENT = os.environ.get("TRACKD_AGENT_NAME", "trackd-langgraph")
PROMPT = "What is 6817 multiplied by 41? Use the calculator tool."


def post(url, token, body):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, resp.read().decode()[:900], time.time() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:1200], time.time() - started
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}", time.time() - started


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT").rstrip("/")
    cred = DefaultAzureCredential()
    token = cred.get_token("https://ai.azure.com/.default").token
    client = AIProjectClient(endpoint=endpoint, credential=cred)

    print("--- session API ---")
    try:
        session = client.agents.create_session(AGENT)
        print(f"SESSION_CREATED id={session.agent_session_id} status={session.status}")
    except Exception as exc:  # noqa: BLE001
        print(f"SESSION_CREATE_ERROR={type(exc).__name__}: {str(exc)[:250]}")

    probe = f"{endpoint}/agents/{AGENT}/responses?api-version=bogus-version"
    status, text, _ = post(probe, token, {"input": PROMPT})
    print("APIVER_PROBE status=%s %s" % (status, text.replace('"', "'").replace("\n", " ")[:400]))

    v1 = f"{endpoint}/openai/v1/responses"
    candidates = [
        ("agent_ref_name", v1, {"agent": {"type": "agent_reference", "name": AGENT}, "input": PROMPT}),
        ("agent_plain", v1, {"agent": {"name": AGENT}, "input": PROMPT}),
        ("agent_str", v1, {"agent": AGENT, "input": PROMPT}),
        ("model_versioned", v1, {"model": f"{AGENT}:3", "input": PROMPT}),
        ("path_agents", f"{endpoint}/agents/{AGENT}/responses", {"input": PROMPT}),
        ("path_agents_v1", f"{endpoint}/openai/v1/agents/{AGENT}/responses", {"input": PROMPT}),
    ]
    for label, url, body in candidates:
        status, text, elapsed = post(url, token, body)
        text = text.replace('"', "'").replace("\n", " ").replace("\r", " ")
        print(f"{label}: status={status} elapsed_s={elapsed:.2f} {text[:900]}")
        if status == 200:
            print(f"RESULT=INVOKE_OK via={label}")
            return 0
    print("RESULT=INVOKE_FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
