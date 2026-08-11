import time, json
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
import _config
ep=_config.get("OAI_PROJECT_ENDPOINT")
oai=AIProjectClient(endpoint=ep, credential=AzureCliCredential()).get_openai_client()
CI=[{"type":"code_interpreter","container":{"type":"auto"}}]
# force real execution: something the model cannot know without running
P=("Execute python code to do this, do not answer from memory: "
   "import hashlib,platform,os,time; print(hashlib.sha256(str(time.time()).encode()).hexdigest()[:16], platform.python_version())")
t0=time.time(); r=oai.responses.create(model="gpt-4.1", tools=CI, input=P); dt=time.time()-t0
print(f"elapsed {dt:.2f}s")
for item in r.output:
    print(" item.type=", getattr(item,'type',None))
    if getattr(item,'type','')=='code_interpreter_call':
        print("   container_id=", getattr(item,'container_id',None))
        print("   code=", str(getattr(item,'code',''))[:150])
        print("   outputs=", str(getattr(item,'outputs',''))[:200])
print("text:", r.output_text[:200])
