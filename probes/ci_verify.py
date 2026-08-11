"""Verify whether the built-in Code Interpreter tool ACTUALLY executes.

Earlier Track A/C numbers may have mixed calls where the model answered from
memory (no tool call) with calls where the sandbox really ran.
"""

import statistics
import time

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
import _config

EP = _config.get("OAI_PROJECT_ENDPOINT")
MODEL = "gpt-4.1"
CI = [{"type": "code_interpreter", "container": {"type": "auto"}}]

EASY = "Use python to compute sum(range(100)). Return only the number."
HARD = (
    "Using the python tool, compute the SHA-256 hex digest of the exact string "
    "'probe-token-{n}' and also the 3000th prime number. You MUST run code; "
    "do not answer from memory. Return only: <digest> <prime>"
)

pc = AIProjectClient(endpoint=EP, credential=AzureCliCredential())
oai = pc.get_openai_client()


def call(prompt):
    t = time.perf_counter()
    r = oai.responses.create(model=MODEL, tools=CI, input=prompt)
    dt = time.perf_counter() - t
    kinds = [getattr(i, "type", "?") for i in r.output]
    fired = any("code_interpreter" in k for k in kinds)
    container = None
    for i in r.output:
        if "code_interpreter" in getattr(i, "type", ""):
            container = getattr(i, "container_id", None)
    return dt, fired, kinds, container, r.output_text[:60].replace("\n", " ")


def bench(label, prompt_fn, n):
    rows = []
    for i in range(n):
        try:
            dt, fired, kinds, cid, txt = call(prompt_fn(i))
            rows.append((dt, fired))
            print(
                f"  {label}[{i}] {dt:6.2f}s fired={fired} kinds={kinds} "
                f"container={str(cid)[:18]} out={txt!r}",
                flush=True,
            )
        except Exception as e:
            print(f"  {label}[{i}] ERROR {type(e).__name__}: {str(e)[:140]}", flush=True)
    hit = [d for d, f in rows if f]
    miss = [d for d, f in rows if not f]
    print(
        f"{label}: tool_fired={len(hit)}/{len(rows)}"
        + (
            f" | fired median={statistics.median(hit):.2f}s "
            f"min={min(hit):.2f}s max={max(hit):.2f}s"
            if hit
            else ""
        )
        + (f" | no-tool median={statistics.median(miss):.2f}s" if miss else "")
    )


print("=== EASY prompt (can be answered from memory) ===")
bench("EASY", lambda i: EASY, 6)

print("\n=== HARD prompt (forces real execution) ===")
bench("HARD", lambda i: HARD.format(n=i), 8)
