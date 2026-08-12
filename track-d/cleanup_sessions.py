"""Track D - delete leftover hosted-agent sessions.

Every POST to the Responses endpoint that does not continue an existing
conversation mints a new agent session, and sessions linger (IDLE, with a
rolling 30-day expiry) until they are deleted. A POC that retries invocations
accumulates them quickly, which muddies later cold-start measurements.

Set TRACKD_CLEANUP_AGENT to restrict cleanup to one agent, and
TRACKD_CLEANUP_APPLY=1 to actually delete (default is a dry run).
"""
import logging
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "probes"))
import _config  # noqa: E402

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402

# The SDK logs a full traceback for every empty (204) delete response under
# "failsafe deserialization". At 128 sessions that floods the log buffer and
# pushes the real errors out of the tail.
logging.getLogger("azure").setLevel(logging.CRITICAL)
logging.getLogger("azure.core").setLevel(logging.CRITICAL)

ONLY_AGENT = os.environ.get("TRACKD_CLEANUP_AGENT", "")
APPLY = os.environ.get("TRACKD_CLEANUP_APPLY", "") == "1"
KEEP = {s for s in os.environ.get("TRACKD_CLEANUP_KEEP", "").split(",") if s}


def main() -> int:
    client = AIProjectClient(
        endpoint=_config.get("OAI_PROJECT_ENDPOINT"), credential=DefaultAzureCredential()
    )
    agents = [ONLY_AGENT] if ONLY_AGENT else [a.name for a in client.agents.list()]
    agents = sorted(set(agents))
    print(f"MODE={'APPLY' if APPLY else 'DRY_RUN'} AGENTS={agents}")

    total = deleted = failed = 0
    for agent in agents:
        try:
            sessions = list(client.agents.list_sessions(agent))
        except Exception as exc:  # noqa: BLE001
            print(f"AGENT={agent} LIST_ERROR={type(exc).__name__}")
            continue
        print(f"AGENT={agent} SESSION_COUNT={len(sessions)}")
        if sessions:
            first = sessions[0]
            keys = list(first.as_dict().keys()) if hasattr(first, "as_dict") else dir(first)
            print(f"  SESSION_KEYS={keys}")
        for session in sessions:
            session_id = (
                getattr(session, "id", None)
                or getattr(session, "agent_session_id", None)
                or getattr(session, "name", "")
            )
            if session_id in KEEP:
                continue
            total += 1
            if not APPLY:
                continue
            try:
                client.agents.delete_session(agent, session_id)
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed <= 3:
                    detail = str(exc).replace(chr(10), " ")[:200]
                    print(f"  DELETE_ERROR id={session_id} {type(exc).__name__}: {detail}")

    print(f"CANDIDATES={total} DELETED={deleted} FAILED={failed}")
    print("RESULT=CLEANUP_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
