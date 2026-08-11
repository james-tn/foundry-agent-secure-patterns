import time
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
import _config
ep=_config.get("OAI_PROJECT_ENDPOINT")
cred=AzureCliCredential()
pc=AIProjectClient(endpoint=ep, credential=cred); oai=pc.get_openai_client()
def run(tag):
    t0=time.time()
    oai.responses.create(model="gpt-4.1", input="Reply with the single word: pong")
    print(f"{tag}: {time.time()-t0:.2f}s", flush=True)
run("warmup"); run("baseline")
for mins in (5,10):
    print(f"--- idle {mins} min ---", flush=True); time.sleep(mins*60)
    run(f"after_{mins}min_1"); run(f"after_{mins}min_2")
