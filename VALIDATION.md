# POC validation review

Independent re-validation performed **2026-08-05 / 06**, after the original
Track A/B/C runs. The goal was to challenge the earlier conclusions, in
particular anything that depended on an SDK or API version.

Two conclusions were **corrected**, one was **materially strengthened**, and the
rest **reproduced**. Details below.

---

## 1. SDK versions — verified current, no change needed

Checked directly against PyPI on 2026-08-05:

| Package | Pinned in POC | Latest on PyPI | Verdict |
|---|---|---|---|
| `azure-ai-projects` | 2.4.0 | **2.4.0** | Current |
| `azure-identity` | 1.25.3 | **1.25.3** | Current |
| `openai` | 2.53.0 | **2.53.0** | Current |
| `azure-ai-agents` | 1.1.0 | 1.1.0 stable (1.2.0b6 preview) | Current stable |

Note the official foundry-sample `code-interpreter-custom/requirements.txt`
pins `azure-ai-projects==2.0.0b2`, which is **four minor releases behind**. The
POC is ahead of the published sample, not behind it. No SDK-driven conclusion
needs revising.

The agent surface used throughout (`agents.create_version` +
`PromptAgentDefinition` + `responses.create(...)` with an `agent_reference`) is
the current v2 Agents/Responses API, not the retired v1 assistants/threads API.

---

## 2. Control-plane API versions — one real correction

`az provider show` advertises these `Microsoft.App/sessionPools` versions:

```text
2026-03-02-preview, 2026-01-01, 2025-10-02-preview, 2025-07-01,
2025-02-02-preview, 2025-01-01, 2024-10-02-preview, 2024-08-02-preview,
2024-02-02-preview, 2023-11-02-preview, 2023-08-01-preview
```

**Correction:** `2026-03-02-preview` is advertised by the resource provider but
is **not published** in `Azure/azure-rest-api-specs`. The newest *published*
preview is **`2025-10-02-preview`**, and the newest published version overall is
the stable **`2026-01-01`**.

More importantly, in the published TypeSpec the MCP property is annotated:

```typespec
@removed(Versions.v2026_01_01)
mcpServerSettings?: McpServerSettings;
```

So `mcpServerSettings` **does not exist in the stable `2026-01-01` contract** —
confirmed empirically (a GET at `2026-01-01` omits the field entirely, while
`2025-10-02-preview` and `2026-03-02-preview` return it).

**Practical guidance:** build session-pool automation against
`2025-10-02-preview` while MCP is required, and treat MCP as preview-only
surface that is currently *absent* from the GA contract. Do not pin
`2026-03-02-preview` — it is unpublished and unsupportable.

Canonical property casing, from the published spec and confirmed by a live GET:

```json
{"isMcpServerEnabled": true,
 "isMcpServerApiKeyDisabled": false,
 "mcpServerEndpoint": "https://<region>.dynamicsessions.io/.../mcp"}
```

`mcpServerEndpoint` is read-only. An open specs PR (#42917) proposes changing
the casing to `isMCPServerEnabled` because "ACA backend response body casing
does not match" — so expect churn here.

---

## 3. CustomContainer MCP — conclusion upheld, evidence now decisive

The original Track C finding said `mcpServerSettings` is silently stripped from
CustomContainer pools. That test used **PATCH**, which is not a valid test:
`mcpServerSettings` is absent from the documented
`SessionPoolUpdatablePropertiesProperties` PATCH model, so it is out of contract
for *every* container type. The original evidence was therefore weak.

It was re-tested properly, removing every variable:

| Variable | Value used in the re-test |
|---|---|
| Path | **PUT** (create-or-replace, the documented path for MCP) |
| API version | **`2025-10-02-preview`** (newest published preview) |
| Image | **`mcr.microsoft.com/k8se/services/codeinterpreter:0.9.18-python3.12`** — the exact image in Microsoft's official sample |
| Body | Byte-for-byte the official `infra.bicep` shape, including `mcpServerSettings: {isMcpServerEnabled: true}` |
| Feature flag | `Microsoft.App/SessionPoolsSupportMCP` = `Registered` |
| Region | East US (the documented region) |

Result:

```text
PUT  -> 201 Created
poll -> provisioningState: Succeeded
GET  -> "mcpServerSettings": null

POST .../fetchMCPServerCredentials
  -> 400 SessionMCPServerNotEnabled:
     "Session pool 'official-mcp-test' does not have MCP Server enabled."
```

Control, same call, same API version, against a **PythonLTS** pool:

```text
POST .../fetchMCPServerCredentials -> 200, returns {"apiKey": ...}
```

**Conclusion (now high confidence): MCP cannot currently be enabled on a
CustomContainer session pool**, even using Microsoft's own sample image and
sample configuration.

This **contradicts Microsoft's own published material**, which makes it a
supportable escalation rather than a documentation-reading error on our side:

- ACA MCP overview states the platform-managed MCP server "exposes all three
  tools **regardless of the session pool's `containerType`**".
- The Foundry "custom code interpreter" article and its official
  `foundry-samples` Bicep set `containerType: 'CustomContainer'` **together
  with** `mcpServerSettings.isMcpServerEnabled: true`.

Both cannot be true at once. Raise this with the product group with the trace
IDs captured above.

**Impact on the recommendation: none.** The Track C guidance already routes
custom-container execution through the application's tool layer rather than
server-side MCP, so the workaround stands unchanged.

---

## 4. Data-plane API — corrected

The original runner used an undocumented legacy form:

```http
POST {poolEndpoint}/execute?api-version=2024-02-02-preview
{"code": "..."}
```

The **documented** contract is the plural route with a top-level body:

```http
POST {poolEndpoint}/executions?api-version=2025-10-02-preview&identifier={id}
{"codeInputType":"Inline","executionType":"Synchronous",
 "code":"...","timeoutInSeconds":60}
```

Probed live across every advertised version. Findings:

- `/execute`, `/code/execute` and `/executions` **all** work, on **all**
  advertised api-versions, with identical latency. The api-version is not
  behaviourally significant on the data plane today.
- `timeoutInSeconds` is documented as required but is in practice optional.
- `/executions` returns the richer documented envelope
  (`status`, `result.stdout`, `result.executionTimeInMilliseconds`).

`track-c/test-runner/run_tests.py` has been **migrated to the documented
`/executions` + `2025-10-02-preview` contract** and re-run successfully. Note
that Microsoft's own conceptual docs are internally inconsistent here
(`session-pool.md` still shows `code/execute` with `2024-02-02-preview`), so
prefer the generated REST reference and TypeSpec.

---

## 5. Track A cold start — reproduced independently

Re-measured on a different day with the latest SDK:

| Measurement | Original | Re-validated | Verdict |
|---|---|---|---|
| Cold process, first call, end to end | 7.12 s | **7.20 s** | Reproduced |
| Entra token acquisition | 1.31 s | 2.23 s | Reproduced (dominant cold cost) |
| Client construction | 0.04 s | 0.06 s | Reproduced |
| Warm steady state (median) | ~2.3 s | **1.73 s** (n=10) | Reproduced |
| First call after 10 min idle | 3.61 s | **2.45 s** | Reproduced |
| Idle penalty vs warm | +1.3 s | **+0.72 s** | Reproduced |

**The headline conclusion stands: there is no evidence of agent de-allocation.**
A ten-minute idle costs well under a second. The ~5 s felt as "cold start" is
client-side credential and connection setup, fixable in the customer's own code
by caching the credential and reusing one long-lived client.

---

## 6. Built-in Code Interpreter — methodology corrected, conclusion stronger

**This is the most important correction in the review.**

The original latency comparison used the prompt
`"Use python to compute sum(range(100)). Return only the number."` Re-testing
shows the model **never actually invokes the tool** for that prompt:

```text
EASY[0..5]  ~1.8s  fired=False  kinds=['message']  out='4950'
EASY: tool_fired=0/6
```

The model answers `4950` from memory. So the earlier "code interpreter" timings
were partly measuring plain model calls, and the apparent gap between "plain
model" and "code interpreter" was understated. Any conclusion drawn from that
comparison was unsound.

Re-tested with a prompt that **cannot** be answered from memory (SHA-256 of a
unique string plus an Nth prime), asserting that a `code_interpreter_call`
appears in the response output:

```text
HARD: tool_fired=8/8 | median=16.40s  min=6.57s  max=103.28s
```

Container reuse was then tested explicitly, since it is the natural
"keep it alive" lever for the built-in tool:

| Mode | n | Median | Min | Max |
|---|---:|---:|---:|---:|
| New container each call (`container: {type: auto}`) | 8 | 16.40 s | 6.57 s | 103.28 s |
| **Explicitly reused `container_id`** | 6 | **17.79 s** | 4.55 s | 52.13 s |

**Reusing the container does not help.** The cost is the extra model round-trips
to author, execute and summarise the code — not sandbox startup. This directly
answers the customer's "keep a few agents alive" question for the built-in tool:
there is no warm-container knob, and pinning one manually buys nothing.

Reliability, measured rather than anecdotal:

- One `400 invalid_request_error` while interleaving auto-container calls.
- In the final in-VNet demo run, **1 of 3** built-in calls failed with
  `APITimeoutError` after 90 s.

Versus the customer-controlled pool in the same run: **0.076 s – 0.574 s**, with
server-side execution of 8–286 ms.

The original headline ("6–199 s with failures") was directionally right but
under-evidenced. It is now measured, tool-execution-verified, and reproducible.

---

## 7. Track B and Track C — re-run end to end

Both demos were re-executed after the changes.

Track C (`./track-c/run-demo.sh`), now on the documented data-plane contract:

```text
CUSTOM_PACKAGES_ELAPSED=0.574s STATUS=Succeeded EXEC_MS=286
    OUTPUT=PACKAGES_OK polars=1.32.3 pyarrow=21.0.0 matplotlib=3.10.5
CUSTOM_EGRESS_ELAPSED=0.076s   STATUS=Succeeded EXEC_MS=28  OUTPUT=EGRESS_BLOCKED
CUSTOM_FILE_ELAPSED=0.224s     STATUS=Succeeded EXEC_MS=8
BUILTIN[0]_ELAPSED=7.19s  BUILTIN_STATUS=TOOL_CALLED
BUILTIN[1]_ELAPSED=90.07s BUILTIN_STATUS=ERROR_APITimeoutError
BUILTIN[2]_ELAPSED=4.69s  BUILTIN_STATUS=TOOL_CALLED
BUILTIN_SUMMARY n=2 median=5.94s min=4.69s max=7.19s
MCP execution 2.85s / MCP egress blocked 2.67s
RESULT=PASSED
```

Track B was re-run unchanged and still passes; see `FINDINGS.md` §2.

---

## 8. PTU — previously unproven, now measured

Track A's headline mitigation ("use Provisioned/PTU to fix the latency tail") was
originally advice, not evidence. It has now been measured with a temporary 15 PTU
`GlobalProvisionedManaged` gpt-4.1 deployment, interleaved A/B against
`GlobalStandard`. See `FINDINGS.md` §1.6 for the full tables.

Headline: **PTU cut the median code-interpreter latency 2–3x and the extreme tail
from 173.88 s to ~30 s.** Plain model calls went from 1.79 s to 1.30 s median with a
much tighter spread.

Caveats now on record: PTU narrows but does not flatten the distribution (one PTU
call still took 29.91 s), and a fresh PTU deployment returns
`400 Bad request for dependent service` for roughly its first two minutes, so it
must be provisioned ahead of a cutover rather than created in a failover path.

Cost is real: **$1.00/PTU/hour** Global (Azure retail price API, eastus2), so the
15 PTU minimum is ~$15/hour pay-as-you-go. The deployment was deleted immediately
after measurement; quota is back to 0 used.

---

## What changed in the repo

- `track-c/test-runner/run_tests.py` — migrated to the documented `/executions`
  data-plane route and `2025-10-02-preview`; built-in Code Interpreter phase now
  takes `BUILTIN_SAMPLES` measurements and only counts runs where a
  `code_interpreter_call` actually fired.
- `probes/revalidate.py` — cold/warm/idle re-measurement harness.
- `probes/ci_verify.py` — proves whether the built-in tool actually executed.
- `probes/ci_reuse.py` — container-reuse A/B.
- `probes/ptu_ab.py` — Standard vs Provisioned (PTU) A/B latency benchmark.
- Runner image rebuilt as `trackc-test-runner:v6`.

## What did not change

SDK pins (already current), Track A's headline conclusion, Track B's
architecture and evidence, and every customer-facing recommendation in
`FINDINGS.md` §3.
