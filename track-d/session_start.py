"""Track D / D2 - measure hosted-agent session start (cold start).

Creates a session explicitly and polls it to active, timing the transition.
This is the honest way to measure cold start: invoking without an explicit
session makes the service mint a NEW session per request, so a naive retry
loop measures nothing and leaks sessions.
"""
import os
import pathlib
import sys
import time
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "probes"))
import _config  # noqa: E402

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.ai.projects.models import VersionRefIndicator  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402

AGENT = os.environ.get("TRACKD_AGENT_NAME", "trackd-langgraph")
VERSION = os.environ.get("TRACKD_AGENT_VERSION", "3")
TIMEOUT = int(os.environ.get("TRACKD_READY_TIMEOUT", "900"))
LABEL = os.environ.get("TRACKD_LABEL", "session-start")


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT")
    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())

    session_id = os.environ.get("TRACKD_SESSION_ID") or uuid.uuid4().hex
    print(f"LABEL={LABEL}")
    print(f"AGENT={AGENT} VERSION={VERSION}")
    print(f"SESSION_ID={session_id}")

    started = time.time()
    try:
        session = client.agents.create_session(
            AGENT,
            version_indicator=VersionRefIndicator(agent_version=VERSION),
            agent_session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"CREATE_ERROR={type(exc).__name__}: {str(exc)[:400]}")
        return 1
    print(f"CREATE_RETURNED status={session.status} after_s={time.time()-started:.2f}")

    deadline = started + TIMEOUT
    last = None
    while time.time() < deadline:
        try:
            current = client.agents.get_session(AGENT, session_id)
        except Exception as exc:  # noqa: BLE001
            print(f"GET_ERROR={type(exc).__name__}: {str(exc)[:200]}")
            return 1
        status = str(current.status)
        if status != last:
            print(f"  t={time.time()-started:7.1f}s status={status}")
            last = status
        if "ACTIVE" in status.upper():
            print(f"TIME_TO_ACTIVE_S={time.time()-started:.2f}")
            print("RESULT=SESSION_ACTIVE")
            return 0
        if "FAILED" in status.upper():
            print(f"RESULT=SESSION_FAILED after_s={time.time()-started:.2f}")
            return 1
        time.sleep(5)

    print(f"RESULT=SESSION_TIMEOUT last_status={last} waited_s={time.time()-started:.0f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
