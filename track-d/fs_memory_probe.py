"""Requirement 6 - does the hosted agent filesystem work as memory?

The sandbox has a writable disk, but a disk is only "memory" if the *next*
turn lands on the same one. Foundry's docs describe per-session persistent
`$HOME`, so the question is what actually binds a request to a session.

This probe runs the same filesystem tool three times:

  turn 1  a fresh conversation
  turn 2  same conversation, chained with `previous_response_id`
  turn 3  a brand new conversation

`instance_id` is minted per process and `prior_writes` counts markers left by
earlier turns, so the result distinguishes "same sandbox" from "same content
replayed by the model". If turn 2 sees turn 1's writes and turn 3 does not,
the filesystem is conversation-scoped memory and nothing more.
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

AGENT = os.environ.get("TRACKD_AGENT_NAME", "trackd-plat")
API_VERSION = os.environ.get("TRACKD_API_VERSION", "2025-11-15-preview")
PROMPT = "Call probe_filesystem and return its raw JSON output verbatim."


def emit(label: str, payload: dict) -> None:
    """The ACA log pipeline truncates raw JSON at '{', so flatten to key=value."""
    for key, value in payload.items():
        print(f"{label}.{key}={value}")


def invoke(url: str, token: str, previous_response_id: str | None):
    body: dict = {"input": PROMPT}
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return None, None, f"HTTP_{exc.code}", time.time() - t0
    return data, data.get("id"), None, time.time() - t0


def extract(data: dict) -> dict:
    """Pull the tool result out of the response rather than trusting prose."""
    for item in data.get("output", []):
        if item.get("type") == "function_call_output":
            try:
                return json.loads(item.get("output", "{}"))
            except json.JSONDecodeError:
                return {}
    return {}


def summarise(result: dict) -> dict:
    home = next((p for p in result.get("paths", []) if p["path"] == "/home/session"), {})
    tmp = next((p for p in result.get("paths", []) if p["path"] == "/tmp"), {})
    return {
        "instance_id": result.get("instance_id"),
        "process_age_s": result.get("process_age_s"),
        "home_prior_writes": home.get("prior_writes"),
        "home_prior_instances": ",".join(home.get("prior_instances") or []) or "-",
        "tmp_prior_writes": tmp.get("prior_writes"),
    }


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT")
    credential = DefaultAzureCredential()
    token = credential.get_token("https://ai.azure.com/.default").token
    url = (
        f"{endpoint.rstrip('/')}/agents/{AGENT}/endpoint/protocols/openai/responses"
        f"?api-version={API_VERSION}"
    )
    print(f"AGENT={AGENT}")

    turns = [("turn1_new_conversation", False),
             ("turn2_same_conversation", True),
             ("turn3_new_conversation", False)]
    previous_response_id = None
    seen = {}

    for label, chain in turns:
        data, response_id, err, elapsed = invoke(
            url, token, previous_response_id if chain else None
        )
        if err:
            print(f"{label}.error={err}")
            return 1
        summary = summarise(extract(data))
        summary["latency_s"] = round(elapsed, 2)
        summary["chained"] = chain
        emit(label, summary)
        seen[label] = summary
        previous_response_id = response_id
        time.sleep(2)

    same_instance = (
        seen["turn1_new_conversation"]["instance_id"]
        == seen["turn2_same_conversation"]["instance_id"]
    )
    survived = (seen["turn2_same_conversation"]["home_prior_writes"] or 0) > 0
    print(f"SAME_SANDBOX_ACROSS_CHAINED_TURNS={same_instance}")
    print(f"FILESYSTEM_SURVIVED_CHAINED_TURN={survived}")
    print("RESULT=FS_MEMORY_MEASURED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
