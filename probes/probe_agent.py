import os, time, sys
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
import _config

ep = _config.get("OAI_PROJECT_ENDPOINT")
cred = AzureCliCredential()
pc = AIProjectClient(endpoint=ep, credential=cred)

t0=time.time()
try:
    agents = list(pc.agents.list_agents())
    print(f"list_agents OK in {time.time()-t0:.2f}s -> {len(agents)} agents")
    for a in agents[:10]:
        print("  -", a.id, a.name, getattr(a,'model',None))
except Exception as e:
    print("list_agents FAILED:", type(e).__name__, str(e)[:400])
