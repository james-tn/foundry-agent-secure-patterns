import time
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
import _config

ep=_config.get("OAI_PROJECT_ENDPOINT")
pc=AIProjectClient(endpoint=ep, credential=AzureCliCredential())
oai=pc.get_openai_client()
print("client:", type(oai).__name__)
print("has responses:", hasattr(oai,'responses'))

# time N sequential simple responses to measure warm vs cold
for i in range(4):
    t0=time.time()
    r=oai.responses.create(model="gpt-4.1-mini" if False else "gpt-4.1", input="Reply with the single word: pong")
    dt=time.time()-t0
    txt = r.output_text if hasattr(r,'output_text') else str(r)[:60]
    print(f"run {i}: {dt:.2f}s -> {txt.strip()[:40]!r}")
