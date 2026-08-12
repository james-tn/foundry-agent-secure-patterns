"""Track D - diagnose hosted agent sessions.

Lists the agent's sessions and streams the hosted container's logs, which is
the only way to see why a session is not becoming ready.
"""
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "probes"))
import _config  # noqa: E402

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402

AGENT = os.environ.get("TRACKD_AGENT_NAME", "trackd-langgraph")
MAX_LOG_LINES = int(os.environ.get("TRACKD_LOG_LINES", "80"))


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT")
    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())

    sessions = []
    try:
        for session in client.agents.list_sessions(AGENT):
            sessions.append(session)
            if len(sessions) > 5:
                continue
            print(
                f"SESSION id={session.agent_session_id} status={session.status} "
                f"created={session.created_at} last_accessed={session.last_accessed_at}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"LIST_SESSIONS_ERROR={type(exc).__name__}: {str(exc)[:300]}")
    print(f"SESSION_COUNT={len(sessions)}")

    for session in sessions[-1:]:
        sid = session.agent_session_id
        print(f"--- logs for {sid} ---")
        try:
            stream = client.agents.get_session_log_stream(AGENT, os.environ.get("TRACKD_AGENT_VERSION", "3"), sid)
            shown = 0
            for chunk in stream:
                text = chunk.decode(errors="replace") if isinstance(chunk, bytes) else str(chunk)
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    print("  LOG " + line.replace('"', "'")[:220])
                    shown += 1
                    if shown >= MAX_LOG_LINES:
                        break
                if shown >= MAX_LOG_LINES:
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"  LOG_ERROR={type(exc).__name__}: {str(exc)[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
