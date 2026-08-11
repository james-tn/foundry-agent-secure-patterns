"""Measure ACA dynamic-session cold start directly (no LLM in the loop)."""
import sys, time, uuid, json, statistics, urllib.request
from azure.identity import AzureCliCredential
import _config

SUB=_config.get("AZ_SUBSCRIPTION"); RG=_config.get("RG_LATENCY")
API="2024-02-02-preview"
cred=AzureCliCredential(); tok=cred.get_token("https://dynamicsessions.io/.default").token

def execute(pool, sid, code="print(1)"):
    url=(f"https://eastus2.dynamicsessions.io/subscriptions/{SUB}/resourceGroups/{RG}"
         f"/sessionPools/{pool}/code/execute?api-version={API}&identifier={sid}")
    body=json.dumps({"properties":{"codeInputType":"inline","executionType":"synchronous","code":code}}).encode()
    req=urllib.request.Request(url, data=body, method="POST",
        headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"})
    t0=time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            r.read(); return time.time()-t0, None
    except Exception as e:
        detail=getattr(e,'read',lambda:b'')()
        return time.time()-t0, f"{type(e).__name__}:{str(e)[:100]} {detail[:200]}"

def bench(pool, n=6):
    print(f"--- {pool}: {n} NEW sessions (cold path) ---")
    lat=[]
    for i in range(n):
        dt,err = execute(pool, f"probe-{uuid.uuid4().hex[:12]}")
        lat.append(dt); print(f"   new session {i}: {dt:.2f}s {err or ''}")
    print(f"   median NEW = {statistics.median(lat):.2f}s")
    sid=f"reuse-{uuid.uuid4().hex[:12]}"
    d0,e0=execute(pool,sid); print(f"   [reuse seed: {d0:.2f}s {e0 or ''}]")
    warm=[]
    for i in range(4):
        dt,err=execute(pool,sid); warm.append(dt); print(f"   reused    {i}: {dt:.2f}s {err or ''}")
    print(f"   median REUSED = {statistics.median(warm):.2f}s")
    return statistics.median(lat), statistics.median(warm)

if __name__=="__main__":
    res={}
    for pool in sys.argv[1:] or ["pool-cold","pool-warm"]:
        res[pool]=bench(pool)
    print("\n== SUMMARY ==")
    for p,(n,w) in res.items(): print(f"  {p}: new={n:.2f}s reused={w:.2f}s delta={n-w:.2f}s")
