import time
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
import _config

ep=_config.get("OAI_PROJECT_ENDPOINT")

t0=time.time(); cred=AzureCliCredential(); t_cred=time.time()-t0
t0=time.time(); tok=cred.get_token("https://ai.azure.com/.default"); t_tok=time.time()-t0
t0=time.time(); pc=AIProjectClient(endpoint=ep, credential=cred); oai=pc.get_openai_client(); t_cli=time.time()-t0
print(f"cred_ctor={t_cred:.2f}s  token_acquire={t_tok:.2f}s  client_ctor={t_cli:.2f}s")

def run(tag, model="gpt-4.1"):
    t0=time.time()
    r=oai.responses.create(model=model, input="Reply with the single word: pong")
    dt=time.time()-t0
    u=getattr(r,'usage',None)
    print(f"  {tag}: {dt:.2f}s tokens_out={getattr(u,'output_tokens','?')}")
    return dt

print("burst A (token already cached, client already built):")
for i in range(3): run(f"A{i}")

print("idle 60s to test de-allocation...")
time.sleep(60)
print("burst B (after 60s idle):")
for i in range(3): run(f"B{i}")
