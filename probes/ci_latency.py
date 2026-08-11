import time, statistics
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
import _config
ep=_config.get("OAI_PROJECT_ENDPOINT")
pc=AIProjectClient(endpoint=ep, credential=AzureCliCredential())
oai=pc.get_openai_client()
CI=[{"type":"code_interpreter","container":{"type":"auto"}}]

def call(tools=None, prompt="Reply with the single word: pong"):
    t0=time.time()
    r=oai.responses.create(model="gpt-4.1", tools=tools, input=prompt) if tools else \
      oai.responses.create(model="gpt-4.1", input=prompt)
    return time.time()-t0, r

CODE="Use python to compute sum(range(100)). Return only the number."
print("== plain model (no tools) ==")
plain=[call()[0] for _ in range(5)]
print("  ", [f"{x:.2f}" for x in plain], f"median={statistics.median(plain):.2f}s")

print("== code_interpreter, new container each call (type=auto) ==")
ci=[call(CI, CODE)[0] for _ in range(5)]
print("  ", [f"{x:.2f}" for x in ci], f"median={statistics.median(ci):.2f}s")

# reuse an explicit container to see if warm
print("== code_interpreter, REUSED container ==")
dt,r = call(CI, CODE)
cid=None
for item in r.output:
    if getattr(item,'type','')=='code_interpreter_call':
        cid=getattr(item,'container_id',None)
print("  first call %.2fs container=%s"%(dt,cid))
if cid:
    reuse=[{"type":"code_interpreter","container":cid}]
    rs=[call(reuse, CODE)[0] for _ in range(4)]
    print("  reused:", [f"{x:.2f}" for x in rs], f"median={statistics.median(rs):.2f}s")
