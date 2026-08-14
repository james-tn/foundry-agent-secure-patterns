"""Requirement 6 - how does each agent type bind to Foundry Memory?

Inspects the shipped SDK models rather than the docs: this preview is thinly
documented and the SDK is what the service actually accepts.
"""
import os
import sys

import azure.ai.projects as projects
from azure.ai.projects import models as m

print(f"F SDK_VERSION={getattr(projects, '__version__', 'unknown')}")


def rest_fields(cls) -> list[str]:
    out = []
    for key, val in vars(cls).items():
        if key.startswith("_"):
            continue
        if type(val).__name__ in ("_RestField", "property"):
            out.append(key)
    return sorted(out)


for name in ("PromptAgentDefinition", "HostedAgentDefinition", "MemorySearchPreviewTool",
             "MemoryStoreDefaultDefinition", "MemoryStoreDefaultOptions", "MemorySearchOptions"):
    cls = getattr(m, name, None)
    if cls is None:
        print(f"F {name}=ABSENT")
        continue
    print(f"F {name}.FIELDS={rest_fields(cls)}")
    doc = (cls.__doc__ or "").strip().replace("\n", " ")
    print(f"F {name}.DOC={doc[:400]}")

# What type does the hosted agent's memory field hold?
hosted = getattr(m, "HostedAgentDefinition")
field = vars(hosted).get("memory")
print(f"F HOSTED_MEMORY_FIELD_TYPE={type(field).__name__}")
for attr in ("_type", "_rest_name", "_module"):
    print(f"F HOSTED_MEMORY.{attr}={getattr(field, attr, None)}")

# Anything named *AgentMemory* / *Memory*Definition that the field might take.
cands = sorted(n for n in dir(m) if "memor" in n.lower() and "Store" not in n)
print(f"F NON_STORE_MEMORY_MODELS={cands}")
for n in cands:
    c = getattr(m, n)
    if isinstance(c, type):
        print(f"F {n}.FIELDS={rest_fields(c)}")

print("F RESULT=MEM_BINDING_INSPECTED")
sys.exit(0)
