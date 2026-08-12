"""Track D / D2 - measure whether pinning a session keeps a hosted agent warm.

The cold-start question the customer asked is really two questions:

1. How long does it take to bring a hosted agent session up from nothing?
   `session_start.py` answers that.
2. Once it is up, can you keep it up and get fast turns? That is what this
   script answers.

It creates one session explicitly, then sends several turns pinned to that
session id via `?agent_session_id=`. Turn 1 pays whatever provisioning cost is
left; turns 2..N are the steady-state latency you would actually serve users
with. The gap between them is the value of a keep-alive strategy.
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
from azure.ai.projects.models import VersionRefIndicator  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402

AGENT = os.environ.get("TRACKD_AGENT_NAME", "trackd-idle")
VERSION = os.environ.get("TRACKD_AGENT_VERSION", "1")
PROMPT = os.environ.get("TRACKD_PROMPT", "Reply with the single word: ok")
TURNS = int(os.environ.get("TRACKD_TURNS", "4"))
API_VERSION = os.environ.get("TRACKD_API_VERSION", "2025-11-15-preview")


def invoke(url: str, token: str, prompt: str, previous_response_id=None, timeout: int = 120):
    payload = {"input": prompt}
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            return resp.status, time.time() - started, body.get("id")
    except urllib.error.HTTPError as exc:
        return exc.code, time.time() - started, None
    except Exception:  # noqa: BLE001
        return -1, time.time() - started, None


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT")
    credential = DefaultAzureCredential()
    client = AIProjectClient(endpoint=endpoint, credential=credential)
    token = credential.get_token("https://ai.azure.com/.default").token

    session_id = os.environ.get("TRACKD_SESSION_ID") or os.urandom(16).hex()
    print(f"AGENT={AGENT} VERSION={VERSION}")
    print(f"SESSION_ID={session_id}")

    started = time.time()
    client.agents.create_session(
        AGENT,
        version_indicator=VersionRefIndicator(agent_version=VERSION),
        agent_session_id=session_id,
    )
    while True:
        session = client.agents.get_session(AGENT, session_id)
        if str(session.status).endswith("ACTIVE"):
            break
        if time.time() - started > 300:
            print("RESULT=SESSION_NEVER_ACTIVE")
            return 1
        time.sleep(1)
    print(f"TIME_TO_ACTIVE_S={time.time() - started:.2f}")

    # The Responses protocol continues a conversation with previous_response_id,
    # not with the agent_session_id query parameter used by the Invocations
    # protocol. Passing agent_session_id here makes the host hang.
    url = (
        f"{endpoint.rstrip('/')}/agents/{AGENT}/endpoint/protocols/openai/responses"
        f"?api-version={API_VERSION}"
    )
    latencies = []
    previous_response_id = None
    for turn in range(1, TURNS + 1):
        status, elapsed, response_id = invoke(url, token, PROMPT, previous_response_id)
        print(f"TURN={turn} STATUS={status} LATENCY_S={elapsed:.2f}")
        if status == 200:
            latencies.append(elapsed)
            previous_response_id = response_id
        time.sleep(1)

    if len(latencies) > 1:
        warm = latencies[1:]
        print(f"FIRST_TURN_S={latencies[0]:.2f}")
        print(f"WARM_TURN_MEAN_S={sum(warm) / len(warm):.2f}")
        print(f"WARM_TURN_MIN_S={min(warm):.2f}")
    print("RESULT=KEEPALIVE_MEASURED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
