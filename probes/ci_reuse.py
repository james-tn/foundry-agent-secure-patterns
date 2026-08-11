"""Does reusing an explicit Code Interpreter container remove the startup cost?

Directly answers the customer's "can we keep a few main agents always alive?"
question for the built-in tool.
"""

import statistics
import time

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
import _config

EP = _config.get("OAI_PROJECT_ENDPOINT")
MODEL = "gpt-4.1"

HARD = (
    "Using the python tool, compute the SHA-256 hex digest of the exact string "
    "'probe-token-{n}' and also the {k}th prime number. You MUST run code; "
    "do not answer from memory. Return only: <digest> <prime>"
)

pc = AIProjectClient(endpoint=EP, credential=AzureCliCredential())
oai = pc.get_openai_client()


def call(tools, prompt):
    t = time.perf_counter()
    r = oai.responses.create(model=MODEL, tools=tools, input=prompt)
    dt = time.perf_counter() - t
    cid = None
    fired = False
    for i in r.output:
        if "code_interpreter" in getattr(i, "type", ""):
            fired = True
            cid = getattr(i, "container_id", None)
    return dt, fired, cid


auto = [{"type": "code_interpreter", "container": {"type": "auto"}}]

print("=== establish a container ===")
dt, fired, cid = call(auto, HARD.format(n=100, k=1500))
print(f"seed: {dt:.2f}s fired={fired} container={cid}")

if not cid:
    raise SystemExit("no container id returned; cannot test reuse")

reuse = [{"type": "code_interpreter", "container": cid}]

print("\n=== REUSED container (explicit container id) ===")
r_times = []
for i in range(6):
    dt, fired, cid2 = call(reuse, HARD.format(n=200 + i, k=1500 + i))
    r_times.append((dt, fired, cid2 == cid))
    print(f"  reuse[{i}] {dt:6.2f}s fired={fired} same_container={cid2 == cid}")

print("\n=== AUTO container (new each time), interleaved control ===")
a_times = []
for i in range(6):
    dt, fired, cid3 = call(auto, HARD.format(n=300 + i, k=1500 + i))
    a_times.append((dt, fired))
    print(f"  auto[{i}]  {dt:6.2f}s fired={fired} container={str(cid3)[:20]}")

rf = [d for d, f, _ in r_times if f]
af = [d for d, f in a_times if f]
if rf:
    print(
        f"\nREUSED  n={len(rf)} median={statistics.median(rf):.2f}s "
        f"min={min(rf):.2f}s max={max(rf):.2f}s"
    )
if af:
    print(
        f"AUTO    n={len(af)} median={statistics.median(af):.2f}s "
        f"min={min(af):.2f}s max={max(af):.2f}s"
    )
