import time, statistics
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
import _config
ep=_config.get("OAI_PROJECT_ENDPOINT")
oai=AIProjectClient(endpoint=ep, credential=AzureCliCredential()).get_openai_client()
P=("Execute python code, do not answer from memory: "
   "import time; print(int(time.time()*1000))")
def run(tools):
    t0=time.time(); r=oai.responses.create(model="gpt-4.1", tools=tools, input=P); dt=time.time()-t0
    cid=None
    for i in r.output:
        if getattr(i,'type','')=='code_interpreter_call': cid=getattr(i,'container_id',None)
    return dt,cid

print("== A: FRESH container every call (container.type=auto) ==")
fresh=[]
for i in range(5):
    dt,cid=run([{"type":"code_interpreter","container":{"type":"auto"}}])
    fresh.append(dt); print(f"   call{i}: {dt:.2f}s cid={str(cid)[-8:]}")
print(f"   median={statistics.median(fresh):.2f}s")

print("== B: REUSED warm container ==")
dt,cid=run([{"type":"code_interpreter","container":{"type":"auto"}}])
print(f"   seed: {dt:.2f}s cid={str(cid)[-8:]}")
warm=[]
for i in range(5):
    dt,_=run([{"type":"code_interpreter","container":cid}])
    warm.append(dt); print(f"   call{i}: {dt:.2f}s")
print(f"   median={statistics.median(warm):.2f}s")
print(f"\n>>> sandbox cold-start delta = {statistics.median(fresh)-statistics.median(warm):.2f}s")
