"""A/B latency: GlobalStandard vs Provisioned (PTU) on gpt-4.1.

Tests the tail that Track A identified as the real production risk: forced
code-interpreter execution, where a shared Standard deployment showed a
4.5s -> 103s spread.

Interleaved so both deployments see the same conditions.
"""

import statistics
import time

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
import _config

EP = _config.get("OAI_PROJECT_ENDPOINT")
STD = "gpt-4.1"
PTU = "gpt41-ptu"
CI = [{"type": "code_interpreter", "container": {"type": "auto"}}]

HARD = (
    "Using the python tool, compute the SHA-256 hex digest of the exact string "
    "'ptu-bench-{n}' and the {k}th prime number. You MUST run code; do not "
    "answer from memory. Return only: <digest> <prime>"
)
PLAIN = "Reply with the single word: pong"

pc = AIProjectClient(endpoint=EP, credential=AzureCliCredential())
oai = pc.get_openai_client()


def call(model, prompt, tools=None):
    t = time.perf_counter()
    kwargs = {"model": model, "input": prompt}
    if tools:
        kwargs["tools"] = tools
    try:
        r = oai.responses.create(**kwargs)
        dt = time.perf_counter() - t
        fired = any("code_interpreter" in getattr(i, "type", "") for i in r.output)
        return dt, fired, None
    except Exception as e:
        return time.perf_counter() - t, False, f"{type(e).__name__}: {str(e)[:80]}"


def report(label, rows):
    ok = [d for d, f, e in rows if e is None]
    errs = [e for _, _, e in rows if e]
    if ok:
        srt = sorted(ok)
        p90 = srt[int(len(srt) * 0.9) - 1] if len(srt) >= 2 else srt[0]
        print(
            f"{label:<26} n={len(ok):<3} median={statistics.median(ok):6.2f}s "
            f"min={min(ok):6.2f}s p90={p90:6.2f}s max={max(ok):7.2f}s "
            f"errors={len(errs)}"
        )
    else:
        print(f"{label:<26} ALL FAILED errors={len(errs)}")
    for e in errs:
        print(f"    ERR {e}")


N_PLAIN = 6
N_CI = 6

print("=== warm-up ===")
for m in (STD, PTU):
    print(f"  {m}: {call(m, PLAIN)[0]:.2f}s")

print(f"\n=== plain model, no tools (interleaved, n={N_PLAIN} each) ===")
p_std, p_ptu = [], []
for i in range(N_PLAIN):
    p_std.append(call(STD, PLAIN))
    p_ptu.append(call(PTU, PLAIN))
report("Standard  plain", p_std)
report("PTU       plain", p_ptu)

print(f"\n=== forced code execution (interleaved, n={N_CI} each) ===")
c_std, c_ptu = [], []
for i in range(N_CI):
    d, f, e = call(STD, HARD.format(n=i, k=1500 + i), CI)
    c_std.append((d, f, e))
    print(f"  STD[{i}] {d:7.2f}s fired={f} {e or ''}", flush=True)
    d, f, e = call(PTU, HARD.format(n=100 + i, k=1500 + i), CI)
    c_ptu.append((d, f, e))
    print(f"  PTU[{i}] {d:7.2f}s fired={f} {e or ''}", flush=True)

print()
report("Standard  code-interp", c_std)
report("PTU       code-interp", c_ptu)
