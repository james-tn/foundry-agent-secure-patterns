"""CustomContainer pool: does readySessionInstances matter? Uses generic session HTTP proxy."""
import json, time, uuid, statistics, urllib.request, sys
from azure.identity import AzureCliCredential
import _config
SUB=_config.get("AZ_SUBSCRIPTION"); RG=_config.get("RG_LATENCY"); API="2024-02-02-preview"
tok=AzureCliCredential().get_token("https://dynamicsessions.io/.default").token
def hit(pool, sid, path="/"):
    url=(f"https://eastus2.dynamicsessions.io/subscriptions/{SUB}/resourceGroups/{RG}"
         f"/sessionPools/{pool}/forward{path}?api-version={API}&identifier={sid}")
    req=urllib.request.Request(url, headers={"Authorization":f"Bearer {tok}"})
    t0=time.time()
    try:
        with urllib.request.urlopen(req,timeout=300) as r: r.read(); return time.time()-t0,None
    except urllib.error.HTTPError as e: return time.time()-t0, f"{e.code} {e.read()[:120]}"
    except Exception as e: return time.time()-t0, f"{type(e).__name__} {str(e)[:120]}"
pool=sys.argv[1] if len(sys.argv)>1 else "pool-custom"
lat=[]
for i in range(6):
    dt,err=hit(pool, f"c-{uuid.uuid4().hex[:10]}")
    lat.append(dt); print(f"  new session {i}: {dt:.2f}s {err or 'OK'}")
ok=[l for l,_ in [(l,None) for l in lat]]
print(f"  median NEW = {statistics.median(lat):.2f}s")
