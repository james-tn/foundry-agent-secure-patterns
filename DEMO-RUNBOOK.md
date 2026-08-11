# Demo runbook — Foundry Agent Service deep dive (60 min)

Rehearsed end-to-end on **2026-08-07**. Both demos passed.

| Demo | Wall clock | Result |
|---|---:|---|
| Track B — private internal API | **54 s** | PASSED |
| Track C — code execution patterns | **96 s** | PASSED |

Total live demo time ≈ **2.5 minutes**. Everything else is discussion.

---

## Before the meeting

### T-30 min — run the pre-flight

```bash
cd ~/projects/misc/agent_service_poc
./preflight.sh
```

Must print `PRE-FLIGHT PASSED`. It checks identity, subscription, the private
Foundry account, the internal API, all four session pools, both demo jobs, and
guards against a stray PTU deployment still billing.

> **Most likely failure, seen in rehearsal:** the Azure CLI default subscription
> silently changes when a second subscription is signed in. Every command then
> fails with `ResourceGroupNotFound`, which looks like the environment was
> deleted. `preflight.sh` auto-corrects this. If it cannot, run:
> `az account set --subscription "$AZ_SUBSCRIPTION"` (see `env.local`).

### T-10 min — pre-warm

Run **Track B once** and discard the output. This warms the agent, the data
proxy and the session pool, and shaves ~10 s off the live run.

```bash
./track-b/run-demo.sh > /dev/null 2>&1
```

### Have open

- Two terminals in the repo root
- `FINDINGS.md` and `VALIDATION.md`
- Azure portal on the secure resource group (network tab of the Foundry account)

---

## 60-minute agenda

| Time | Segment |
|---|---|
| 0–5 | Framing and what we built |
| 5–20 | **Security** — VNet and internal API access (Track B demo) |
| 20–35 | **Cold start** — what the 5–10 s actually is (Track A + PTU data) |
| 35–50 | **Code execution** — ACA patterns (Track C demo) |
| 50–60 | Open items, product gaps, next steps |

---

## 0–5 — Framing

Say: we took their three questions and built a working POC in their scenario
rather than answering from documentation. Everything shown is measured, and the
numbers were independently re-validated against the latest SDK and API versions
(`VALIDATION.md`).

One honest framing point that sets the tone: **we found and corrected two of our
own conclusions** during that re-validation. Lead with that — it buys credibility
for everything else.

---

## 5–20 — Security: VNet and internal API access

### Run it

```bash
./track-b/run-demo.sh
```

**Expect (~54 s):**

```text
PRIVATE_API_HEALTH={"status":"ok","visibility":"private-vnet-only",...}
UNAUTHENTICATED_REQUEST=BLOCKED_401
AGENT_RESPONSE=The current status of envelope env-1001 is ... Completed
RESULT=PASSED: private OpenAPI tool returned the internal envelope status
```

### Talking points

- The agent reached an API with **no public DNS record**, in an internal-only ACA
  environment, from a Foundry account with `publicNetworkAccess: Disabled`.
- The API key never appears in source or in the prompt. It lives in a Foundry
  **`CustomKeys` project connection**; the data proxy injects it.
- Three negative tests prove the isolation is real, not implied:
  unauthenticated → **401**; external DNS → **unresolvable**; external Foundry
  data plane → **403**.
- This maps directly onto a real internal API: swap the stub for the production
  hostname and add enterprise DNS forwarding / ExpressRoute routes.

### Their likely question — "can we drop the API key?"

Yes. Use managed identity or OAuth/OBO where the internal API supports Entra.
The key pattern is the fallback for legacy APIs. Put APIM in front once there is
more than one internal API.

---

## 20–35 — Cold start (no demo, data only)

This is the segment where we **correct their premise**, so lead with the answer.

> We could not reproduce a 5–10 s cold start caused by agent de-allocation.
> Nothing is being de-allocated.

| Measurement | Result |
|---|---|
| First call after **10 minutes idle** | **2.45 s** vs 1.73 s warm → **+0.72 s** |
| Cold process, first call | 7.20 s |
| — of which Entra token acquisition | 2.23 s |
| Brand-new PythonLTS session | **0.33–0.36 s** |

So the ~5 s they feel on the first call is **client-side**: credential
acquisition plus TLS/connection setup. Fixed in their own code by caching the
credential and reusing one long-lived client. No Azure change, no cost.

### The real risk is the tail, and PTU fixes it

Forced code-interpreter execution, interleaved A/B on the same account:

| Deployment | Median | Max |
|---|---:|---:|
| GlobalStandard | 15–30 s | **173.9 s** |
| **PTU (15 units)** | **8.8–9.5 s** | **20.9–29.9 s** |

**PTU cut the median 2–3x and the extreme tail from 174 s to ~30 s.**

Be straight about the two caveats — they are the credibility moments:

1. PTU **narrows but does not flatten** the distribution. One PTU call still took
   29.9 s. Code-interpreter orchestration adds variance independent of capacity.
2. A **fresh PTU deployment 400s for ~2 minutes**
   (`Bad request for dependent service`). Provision ahead of a cutover; never
   create it inside a failover path.

Cost: **$1.00/PTU/hour** Global in eastus2 → ~$15/hour at the 15 PTU minimum,
materially cheaper reserved ($260/PTU/month). Recommend PTU for the
latency-critical path with Standard as spillover.

### Also worth stating

`readySessionInstances` is **silently ignored on PythonLTS pools** on every API
version tested. The documented warm-pool knob only does something when you bring
your own container — which is also the only case where it is needed. Do not
design around warming the built-in Python pool.

---

## 35–50 — Code execution patterns

### Run it

```bash
./track-c/run-demo.sh
```

**Expect (~96 s):**

```text
CUSTOM_PACKAGES_ELAPSED=0.72s  OUTPUT=PACKAGES_OK polars=1.32.3 pyarrow=21.0.0 ...
CUSTOM_EGRESS_ELAPSED=0.08s    OUTPUT=EGRESS_BLOCKED URLError
CUSTOM_FILE_ELAPSED=0.21s      OUTPUT=FILE_DATA_URI_OK data:text/csv;base64,...
BUILTIN[0..2]_ELAPSED=...      BUILTIN_SUMMARY n=3 median=15.21s
MCP_EXECUTION_OK result=4950   /  MCP_EGRESS_BLOCKED URLError
RESULT=PASSED
```

### The money slide

The contrast lands on its own — same run, same tenant:

| | Self-controlled ACA pool | Built-in Code Interpreter |
|---|---:|---:|
| Latency | **0.08–0.72 s** | **15.2 s median** |
| Package versions | Pinned by customer | Platform-controlled |
| Egress | Explicit `EgressDisabled` | Platform-controlled |
| Concurrency | Explicit `maxConcurrentSessions` | Platform quota |

That is roughly a **20–200x** difference, plus deterministic packages and
provable egress control.

### Recommend

- Built-in Code Interpreter → low-volume, non-critical convenience tasks.
- PythonLTS via MCP → agent-side execution without custom packages.
- **Controlled CustomContainer pool → deterministic packages, strict egress,
  latency-sensitive work.** This is the one to build on.

### Capacity is the real design constraint, not warmth

Every distinct session identifier holds a slot for the **full cooldown period**.
With `maxConcurrentSessions: 5` and a 600 s cooldown we reproduced hard `429`s.
Size `maxConcurrentSessions` against **arrival rate x cooldown**, reuse one
identifier per user/conversation, and handle 429 explicitly rather than treating
it as a slow cold start.

---

## 50–60 — Open items and next steps

### Product gap to disclose

**MCP cannot currently be enabled on a CustomContainer session pool.** We tested
this properly — PUT (not PATCH), newest published preview `2025-10-02-preview`,
Microsoft's own sample image, feature flag registered, documented region. Pool
provisions `Succeeded` but `mcpServerSettings` stays `null` and
`fetchMCPServerCredentials` returns `SessionMCPServerNotEnabled`. A PythonLTS
pool returns an `apiKey` from the identical call.

This contradicts Microsoft's own docs and sample, so present it as **a known
platform gap being followed up**, with the workaround already in place: invoke
the custom pool from the application's tool layer instead of server-side MCP.
Their architecture does not change.

Also flag: `mcpServerSettings` is **absent from the GA `2026-01-01` contract**
(`@removed`). Pin `2025-10-02-preview` if MCP is required, and do not pin
`2026-03-02-preview` — the RP advertises it but it is unpublished and
unsupportable.

### Suggested next steps to offer

1. Re-point Track B at the real internal hostname over ExpressRoute.
2. Decide PTU sizing against their actual peak concurrency.
3. Move the custom interpreter image to Premium ACR + private endpoint.
4. Load test for `maxConcurrentSessions` sizing.

---

## If something fails live

| Symptom | Response |
|---|---|
| `ResourceGroupNotFound` on everything | Wrong subscription. `az account set --subscription "$AZ_SUBSCRIPTION"` |
| Built-in Code Interpreter times out or 400s | **Do not apologise — this is the finding.** It failed 1 of 3 in an earlier run. Point at the controlled pool's 0.08 s in the same output. |
| Job status stuck `Running` | Normal; Track C takes ~96 s. Talk over it. |
| Agent returns wrong/empty answer | Runner retries 3x automatically. If it still fails, show `FINDINGS.md` §3. |
| Whole demo fails | Fall back to the RESULTS.md files — every number is recorded with its evidence. |

**Do not** create a PTU deployment live. It bills at $15/hour and takes ~2 minutes
to become usable. Use the recorded numbers.

---

## After the meeting

Decide whether to keep the environment running. Idle cost is driven by Standard
Azure AI Search, Cosmos DB, Premium ACR, private endpoints, Log Analytics, the
two ready custom interpreter instances, and one ACA API replica. If you do not
need a repeat demo, delete both resource groups named in
`env.local` (`RG_SECURE` and `RG_LATENCY`).
