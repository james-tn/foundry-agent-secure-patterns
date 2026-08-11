from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
ep=_config.get("OAI_PROJECT_ENDPOINT")
pc=AIProjectClient(endpoint=ep, credential=AzureCliCredential())
try:
    ags=list(pc.agents.list())
    print("agents:",len(ags))
    for a in ags[:8]: print("  ", getattr(a,'name',None), getattr(a,'id',None))
except Exception as e: print("list agents ERR:",type(e).__name__,str(e)[:300])

oai=pc.get_openai_client()
# try code_interpreter tool via responses
import time
import _config
t0=time.time()
try:
    r=oai.responses.create(model="gpt-4.1",
        tools=[{"type":"code_interpreter","container":{"type":"auto"}}],
        input="Use python to compute the 30th Fibonacci number. Return only the number.")
    print(f"code_interpreter OK {time.time()-t0:.2f}s ->", r.output_text[:120])
except Exception as e:
    print("code_interpreter ERR:",type(e).__name__,str(e)[:500])
