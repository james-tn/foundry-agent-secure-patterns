"""Independent re-validation of Track A conclusions using the latest SDK.

Run: .venv/bin/python probes/revalidate.py
"""

import json
import statistics
import subprocess
import sys
import time

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
import _config

EP = _config.get("OAI_PROJECT_ENDPOINT")
MODEL = "gpt-4.1"
CI_TOOLS = [{"type": "code_interpreter", "container": {"type": "auto"}}]
CODE_PROMPT = "Use python to compute sum(range(100)). Return only the number."


def versions():
    import azure.ai.projects as p
    import azure.identity as i
    import openai

    return {
        "azure-ai-projects": p.__version__,
        "azure-identity": i.__version__,
        "openai": openai.__version__,
    }


def main():
    print("SDK VERSIONS:", json.dumps(versions()))

    t0 = time.perf_counter()
    cred = AzureCliCredential()
    t_cred = time.perf_counter() - t0

    t0 = time.perf_counter()
    cred.get_token("https://ai.azure.com/.default")
    t_tok = time.perf_counter() - t0

    t0 = time.perf_counter()
    pc = AIProjectClient(endpoint=EP, credential=cred)
    oai = pc.get_openai_client()
    t_cli = time.perf_counter() - t0

    def call(tools=None, prompt="Reply with the single word: pong"):
        t = time.perf_counter()
        kwargs = {"model": MODEL, "input": prompt}
        if tools:
            kwargs["tools"] = tools
        r = oai.responses.create(**kwargs)
        return time.perf_counter() - t, r

    t_first, _ = call()
    cold_total = t_cred + t_tok + t_cli + t_first
    print(
        f"COLD cred={t_cred:.2f}s token={t_tok:.2f}s client={t_cli:.2f}s "
        f"first_call={t_first:.2f}s TOTAL={cold_total:.2f}s"
    )

    warm = [call()[0] for _ in range(10)]
    print(
        f"WARM n=10 median={statistics.median(warm):.2f}s "
        f"min={min(warm):.2f}s max={max(warm):.2f}s "
        f"vals={[round(x, 2) for x in warm]}"
    )

    ci = []
    for i in range(10):
        try:
            dt, _ = call(CI_TOOLS, CODE_PROMPT)
            ci.append(dt)
            print(f"  CI[{i}] {dt:.2f}s", flush=True)
        except Exception as e:
            print(f"  CI[{i}] ERROR {type(e).__name__}: {str(e)[:120]}", flush=True)
    if ci:
        print(
            f"BUILTIN_CI n={len(ci)} median={statistics.median(ci):.2f}s "
            f"min={min(ci):.2f}s max={max(ci):.2f}s "
            f"vals={[round(x, 2) for x in ci]}"
        )

    idle = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    print(f"IDLE sleeping {idle}s ...", flush=True)
    time.sleep(idle)
    t_idle, _ = call()
    print(f"AFTER_IDLE_{idle}s={t_idle:.2f}s  (warm median {statistics.median(warm):.2f}s)")


if __name__ == "__main__":
    main()
