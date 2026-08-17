"""Requirement 6 - is Foundry Memory usable as long-term memory, in a VNet?

The filesystem probe showed the sandbox disk is conversation-scoped, so
anything that must outlive a conversation needs a store. Foundry Memory is the
managed answer, but memory stores do not support VNet integration.

The caller running this probe needs Foundry User and Cognitive Services OpenAI
User at the Foundry project scope. A deployed hosted agent needs both roles on
its runtime identity.

So this measures three separate things rather than assuming:

  1. can a memory store even be created from inside the private VNet?
  2. does it actually recall a fact stated in a *different* conversation?
  3. is one user's memory isolated from another's?

Step 2 is the real long-term-memory claim. The probe deliberately writes facts
in one scope and reads them back with a differently-worded query, so a hit
means retrieval rather than string matching.
"""
import json
import os
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "probes"))
import _config  # noqa: E402

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402

STORE = os.environ.get("TRACKD_MEMORY_STORE", "poc-longterm-memory")
CHAT = os.environ.get("TRACKD_MEMORY_CHAT", "gpt-4o-mini")
EMBED = os.environ.get("TRACKD_MEMORY_EMBED", "text-embedding-3-small")
SCOPE_A = "user-alice"
SCOPE_B = "user-bob"


def emit(label: str, payload: dict) -> None:
    for key, value in payload.items():
        print(f"{label}.{key}={value}")


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT")
    client = AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )
    memory = client.beta.memory_stores
    print(f"STORE={STORE} CHAT={CHAT} EMBED={EMBED}")

    # 1. Create - this is where a VNet restriction would surface.
    t0 = time.time()
    try:
        from azure.ai.projects.models import (
            MemoryStoreDefaultDefinition,
            MemoryStoreDefaultOptions,
        )

        definition = MemoryStoreDefaultDefinition(
            chat_model=CHAT,
            embedding_model=EMBED,
            options=MemoryStoreDefaultOptions(
                user_profile_enabled=True,
                chat_summary_enabled=True,
            ),
        )
        try:
            store = memory.create(name=STORE, definition=definition,
                                  description="Requirement 6 long-term memory probe")
            created = "created"
        except Exception as exc:  # noqa: BLE001
            if "exist" not in str(exc).lower():
                raise
            store = memory.get(STORE)
            created = "already_existed"
        print(f"CREATE_RESULT={created}")
        print(f"CREATE_ELAPSED_S={time.time() - t0:.2f}")
        print(f"STORE_KIND={getattr(getattr(store, 'definition', None), 'kind', '?')}")
    except Exception as exc:  # noqa: BLE001
        print(f"CREATE_RESULT=FAILED {type(exc).__name__}: {str(exc)[:300]}")
        print("RESULT=MEMORY_CREATE_BLOCKED")
        return 1

    # 2. Conversation one: state facts that a later conversation should recall.
    conversation = [
        {"type": "message", "role": "user", "content":
            "I'm Alice from the contracts team. I always want NDAs routed to "
            "legal-review@example.internal before sending, and I prefer envelopes "
            "expire after 14 days."},
        {"type": "message", "role": "assistant", "content":
            "Understood - NDAs go to legal-review@example.internal first, and "
            "envelopes expire after 14 days."},
    ]
    t1 = time.time()
    poller = memory.begin_update_memories(STORE, scope=SCOPE_A, items=conversation)
    try:
        update = poller.result()
    except Exception as exc:  # noqa: BLE001
        # The LRO failure body carries the real reason; flatten it so the ACA
        # log pipeline does not truncate it at the first '{'.
        import base64
        detail = " ".join(str(exc).split())
        # The ACA log pipeline mangles punctuation, so ship it base64-encoded.
        blob = base64.b64encode(detail.encode()).decode()
        print(f"UPDATE_RESULT=FAILED {type(exc).__name__}")
        for i in range(0, min(len(blob), 2400), 300):
            print(f"UPDATE_DETAIL_B64[{i // 300}]={blob[i:i + 300]}")
        print("RESULT=MEMORY_UPDATE_FAILED")
        return 1
    print(f"UPDATE_ELAPSED_S={time.time() - t1:.2f}")
    print(f"UPDATE_STATUS={getattr(update, 'status', '?')}")

    items = list(memory.list_memories(STORE, scope=SCOPE_A))
    print(f"MEMORY_ITEM_COUNT={len(items)}")
    for i, item in enumerate(items[:5]):
        text = str(getattr(item, "content", getattr(item, "text", item)))
        print(f"MEMORY_ITEM[{i}].kind={getattr(item, 'kind', '?')}")
        print(f"MEMORY_ITEM[{i}].text={text[:180]}")

    # 3. A *different* conversation asks in different words. This is the test.
    t2 = time.time()
    hits = memory.search_memories(
        STORE, scope=SCOPE_A,
        items="What should I do with a new non-disclosure agreement?",
    )
    results = list(getattr(hits, "results", None) or getattr(hits, "data", []) or [])
    emit("SEARCH_SAME_USER", {
        "elapsed_s": round(time.time() - t2, 2),
        "hit_count": len(results),
    })
    for i, hit in enumerate(results[:3]):
        text = str(getattr(hit, "content", getattr(hit, "text", hit)))
        print(f"SEARCH_HIT[{i}]={text[:200]}")

    # 4. A different user must not see Alice's memories.
    other = memory.search_memories(
        STORE, scope=SCOPE_B,
        items="What should I do with a new non-disclosure agreement?",
    )
    other_results = list(getattr(other, "results", None) or getattr(other, "data", []) or [])
    print(f"SEARCH_OTHER_USER_HITS={len(other_results)}")

    print(f"LONG_TERM_RECALL_WORKS={len(results) > 0}")
    print(f"SCOPE_ISOLATION_HOLDS={len(other_results) == 0}")
    print("RESULT=MEMORY_MEASURED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
