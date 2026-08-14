# Azure AI Foundry Agent Service — POC findings

A hands-on POC that deployed a network-isolated Foundry Agent Service
environment and measured its real behaviour, to answer three questions that come
up in nearly every enterprise evaluation:

1. Can agents reach **private internal APIs** from inside a VNet?
2. Is there a **cold-start / de-allocation** penalty, and can agents be kept alive?
3. What are the viable **code-execution** patterns and their trade-offs?

Everything below was **measured live in Azure**, not sourced from documentation.

- Built **2026-08-04 → 08-05** · re-validated **08-06** · demos rehearsed **08-07**
- Two regions, `gpt-4.1`, Standard Agent Setup with network injection

> During re-validation we **disproved two of our own conclusions**. Both are
> documented in [`VALIDATION.md`](VALIDATION.md) rather than quietly corrected.

> **Want the reusable guidance rather than the evidence?**
> [`SECURE-AGENT-GUIDELINES.md`](SECURE-AGENT-GUIDELINES.md) is a standalone
> guideline for deploying Foundry agents securely in any enterprise — private
> networking, reaching internal and on-premises APIs, latency, and code
> execution. It contains **no customer, subscription or tenant specifics** and
> every claim is tagged Verified / Documented / Inferred. Share that file on its
> own; this repo is the evidence behind it.

---

## The three questions, answered

### 1. Security — VNet setup and reaching internal APIs from the agent service

**Yes, and it is proven end-to-end.**

A Foundry prompt agent called an API that has **no public DNS record**, hosted in
an internal-only Azure Container Apps environment, from a Foundry account with
`publicNetworkAccess: Disabled`. The API key never appeared in source code or in
the prompt — it lives in a Foundry `CustomKeys` project connection and is
injected by the agent data proxy.

Isolation was proven with negative tests, not asserted:

| Test | Result |
|---|---|
| Authenticated agent tool call | **200** — returned the internal record status |
| Unauthenticated call to same route | **401** |
| External DNS resolution of the API host | **Unresolvable** |
| External call to the Foundry data plane | **403** |

> **Decision to make early:** network injection **cannot be added to an existing
> public Foundry account**. If you have been prototyping on a public account, the
> production account is a rebuild, not a setting change.

**If the internal API is on-premises or in another VNet** — the realistic
enterprise case — the Foundry configuration is unchanged. What changes is only
how the packet leaves the VNet (peering / ExpressRoute / VPN, optionally through
a firewall via a UDR on the agent subnet) and how the name resolves (Azure DNS
Private Resolver with a conditional-forwarding ruleset to corporate DNS). Two
traps: **do not enable TLS inspection** on that path, and allow the
`AzureActiveDirectory` service tag for managed-identity token acquisition.
Recommended shape is APIM in internal VNet mode as the single private ingress.

Setup checklist and hybrid guidance:
[`SECURE-AGENT-GUIDELINES.md`](SECURE-AGENT-GUIDELINES.md) Parts 1–2.
Evidence: [`FINDINGS.md`](FINDINGS.md) §2.

---

### 2. Cold start — is there a de-allocation penalty, and can agents be kept alive?

**The premise does not hold. There is no agent de-allocation.**

| Measurement | Result |
|---|---|
| First call after **10 minutes idle** | **2.45 s** vs 1.73 s warm → **+0.72 s** |
| Cold process, first call, end to end | 7.20 s |
| — of which Entra token acquisition | 2.23 s |
| Brand-new PythonLTS sandbox session | **0.33–0.36 s** |

The ~5 s felt on the first call is **client-side** — credential acquisition plus
TLS/connection setup. It is fixed in application code by caching the
credential and reusing one long-lived HTTP client. No Azure change, no cost.

**"Keep agents alive" is the wrong lever.** We tested it directly: explicitly
reusing a code-interpreter container gave **no benefit** (median 17.79 s reused
vs 16.40 s fresh). Warming buys nothing because the sandbox was never the
bottleneck — the cost is the extra model round-trips to author, execute and
summarise code.

**The real risk is the latency tail, and PTU measurably fixes it.** Interleaved
A/B on the same account, forced code execution:

| Deployment | Median | Max |
|---|---:|---:|
| Global Standard | 15–30 s | **173.9 s** |
| **Provisioned (15 PTU)** | **8.8–9.5 s** | **20.9–29.9 s** |

Caveats we recorded honestly: PTU **narrows but does not flatten** the
distribution (one PTU call still took 29.9 s), a fresh PTU deployment returns
`400 Bad request for dependent service` for roughly its first two minutes, and
it costs **$1.00/PTU/hour** Global (~$15/hr at the 15-unit minimum).

Evidence: [`FINDINGS.md`](FINDINGS.md) §1 (§1.6 for PTU)

---

### 3. Code execution — patterns and trade-offs with Azure Container Apps

**A self-controlled ACA session pool wins decisively over the built-in tool.**

> **What "controlled ACA pool" means.** An [Azure Container Apps *dynamic
> session pool*](https://learn.microsoft.com/en-us/azure/container-apps/sessions)
> that **you** create in **your own** subscription and attach to the agent as a
> tool, instead of using Foundry's built-in Code Interpreter. Each run gets a
> Hyper-V–isolated sandbox, but you own the container image, the installed
> packages, the network policy, the CPU/memory size, concurrency and session
> lifetime. The built-in Code Interpreter is the opposite trade-off: zero setup,
> but Microsoft owns the image, the packages and the network posture — so you
> cannot pin a library version or prove egress is blocked.
>
> There are two pool types, and the difference matters:
>
> | Pool type | Image | Choose when |
> |---|---|---|
> | `PythonLTS` (managed) | Microsoft's Python image | You want isolation and concurrency control, but not custom packages. **MCP works here** |
> | `CustomContainer` | **Yours, from your registry** | You must pin versions, add private libraries, or prove egress is disabled |
>
> "Controlled" in this repo always means **`CustomContainer`** — our image pins
> `polars`, `pyarrow` and `matplotlib`, and sets `EgressDisabled`.

Same run, same environment:

| | Controlled ACA pool | Built-in Code Interpreter |
|---|---:|---:|
| Latency | **0.08 – 0.72 s** | **15.2 s median** |
| Package versions | Pinned by you | Platform-controlled |
| Runtime egress | Explicit `EgressDisabled` | Platform-controlled |
| Concurrency | Explicit `maxConcurrentSessions` | Platform quota |
| Reliability | 0 failures observed | 1 of 3 calls failed (90 s timeout) in one run; HTTP 400s seen |

That is a **20–200x** latency difference, plus deterministic packages and
provable egress control — which matter more than latency for a compliance story.

**The real design constraint is concurrency, not warmth.** Every distinct session
identifier holds a slot for the **full cooldown period**. With
`maxConcurrentSessions: 5` and a 600 s cooldown we reproduced hard `429`s. Size
`maxConcurrentSessions` against **arrival rate × cooldown**, reuse one identifier
per user/conversation, and handle 429 explicitly rather than treating it as a
slow cold start.

Evidence: [`FINDINGS.md`](FINDINGS.md) §3

---

## Requirements 4 to 6 — would hosted agents change any of this?

The three requirements above were answered with **prompt agents**. The same
three — plus later ones on request-context propagation, using an existing
multi-provider LLM gateway, and a follow-up list covering telemetry, library
integration and filesystem memory — were then re-run with **hosted agents**
(own code, LangGraph harness) in the same locked-down account.

| | Prompt agent | Hosted agent |
|---|---|---|
| VNet + internal API | Works; credential injected by the platform | Works, but the agent identity starts with **zero RBAC** and deployment is a **data-plane** operation, so CI/CD must run in-VNet |
| Cold start | No measurable penalty | **~15 s per new session**, and `ACTIVE` does not mean ready to serve |
| Code execution | Sandboxed pool, ~0.57–5.94 s | In-process, **0.15 ms**, but **no isolation** from the agent's own identity and secrets |
| Request context (identity, tenant, correlation) | **W3C `traceparent` and `baggage` measured reaching the internal API**; `x-client-*` and `metadata` do not survive; per-user auth via Toolbox/MCP | **`x-client-*` headers, `metadata` and W3C `traceparent` measured working end to end** |
| Existing LLM gateway (multi-provider) | Needs an admin-created **`ModelGateway`** connection; model is `<connection>/<model>` | Set `base_url` in your own client — no connection, and no governance either |
| Custom telemetry and metrics | Microsoft's traces only — **no custom dimensions or metrics**, measured; but spans carry **your** trace id as `operation_Id` | **Custom spans and metrics measured landing in App Insights** with DocuSign dimensions |
| Telemetry to a non-Azure backend (Datadog, Splunk, OTLP collector) | No | **Yes — measured**: traces, metrics and logs delivered to a third-party OTLP sink |
| Prompt-optimisation libraries (DSPy) | Not possible inside the loop | **DSPy 3.3.0 installed and ran** against the Foundry model |
| Filesystem memory | n/a | Writable 4.1 GB disk; **persists per conversation, not across them** |

**On OBO:** neither agent type can perform a generic on-behalf-of exchange to an
arbitrary internal API — the caller's `Authorization` header is never delivered
to agent code. Per-user delegated access is available through **Toolbox/MCP
connections** (`oauth2`, `user-entra-token`); anything else needs a token-broker
or a server-side context store.

**On the LLM gateway:** the customer's driver is reaching several providers,
naming Azure OpenAI and **Google Gemini**. Enumerating `eastus2` returns eleven
publishers — Anthropic, Meta, Mistral, DeepSeek, xAI, Cohere and others — but
**no Google**. So every other named provider can be used natively with no
gateway at all, while Gemini can only be reached *through* one. Both agent types
were measured working against a real gateway deployed in the VNet, including
tool calling. The trap: Foundry's BYOM path **always requests streaming**, so a
gateway that only speaks non-streaming JSON fails with an opaque `500`.

**On the follow-up requirements:** custom telemetry, DSPy and filesystem memory
were measured directly. A hosted agent emitted its own OpenTelemetry span and
metrics with DocuSign dimensions and both were queried back out of Application
Insights; **DSPy 3.3.0** installed and ran a real program against the Foundry
model; and the sandbox has a writable 4.1 GB disk that **persists across
chained turns but not across conversations** — so the *filesystem* is
short-term memory only. All three are hosted-agent-only.

Long-term memory is meant to be **Foundry Memory**, and that was tested too.
A memory store was created successfully from inside the VNet, but **ingestion
failed with a 401** from the Memory service to its own model deployment — and
the identical failure reproduced on a **fully public** project and survived
three different RBAC grants, so it is not a networking or permissions problem.
Memory is preview; treat long-term memory as unproven and keep the measured
fallback of conversation state in the customer's own Cosmos DB. Multi-language execution lives in
session pools, where the resource provider accepts `PythonLTS`, `NodeLTS`,
`Shell`, `CsharpLTS`, `GpuBase` and `CustomContainer` — the last two of which
are undocumented.

**The gateway can be the customer's own, and can stay private.** It does not
have to be Azure API Management or any Azure product — the one measured here is
a stdlib-only Python server, and the connection simply takes its URL. It also
resolved to a **private IP** inside the VNet, unreachable from the public
internet, and Foundry reached it regardless. The full endpoint contract is in
[`FINDINGS.md`](FINDINGS.md) §4.5.

Full requirement-by-requirement comparison, capability matrix and the twenty-seven
operational gotchas: [`HOSTED-VS-PROMPT-AGENTS.md`](HOSTED-VS-PROMPT-AGENTS.md)

---

## The insight that matters most

**"Keep the agents warm" optimizes the wrong variable.** It is a non-problem. The
three actual risks are different things with different fixes:

| Real risk | Actual fix |
|---|---|
| Client initialisation (~5 s, first call only) | Cache credential, reuse HTTP client — free |
| **Model capacity tail (4.5 s → 174 s)** | **Provisioned throughput (PTU)** |
| Session concurrency (hard 429s) | Size `maxConcurrentSessions` = arrival rate × cooldown |

The tail is the one that hurts in production, and it is **invisible in a POC that
only measures averages**. A 5–10 s cold-start concern is real but misattributed.

## Platform notes and current limitations

Four behaviours we hit that are **not obvious from the documentation**. None of
them block the architecture in this repo, but each is worth knowing before you
design around it — and each has a workaround.

### 1. MCP cannot be enabled on CustomContainer session pools — contradicts published docs

**Context, because this is easily misread.** MCP is a general tool protocol —
if you already have an MCP server, you attach it to an agent as ordinary
configuration and none of this applies. This finding is narrower: an ACA session
pool can ship a **pre-built MCP server** in front of the code sandbox, so an
agent can run code without you writing a server. There the **pool is the MCP
server** (exposing `launchPythonEnvironment`, `runPythonCodeInRemoteEnvironment`)
and the agent is the client — which is why `mcpServerSettings` is a *pool*
property and `containerType` matters. Losing it costs convenience, not
capability: you can still front the pool with your own MCP server, an OpenAPI
tool, or direct `/executions` calls.

Tested along the fully documented path, with every variable removed:

- **PUT** (create-or-replace), not PATCH — `mcpServerSettings` is absent from the
  documented PATCH model, so PUT is the only in-contract way to set it
- API version **`2025-10-02-preview`** — the newest *published* preview
- Image **`mcr.microsoft.com/k8se/services/codeinterpreter:0.9.18-python3.12`** —
  the exact image from Microsoft's official sample
- `Microsoft.App/SessionPoolsSupportMCP` = **Registered**, documented region (East US)

Result:

```text
PUT  -> 201 Created   (request body contained mcpServerSettings.isMcpServerEnabled: true)
poll -> provisioningState: Succeeded
GET  -> "mcpServerSettings": null
POST .../fetchMCPServerCredentials -> 400 SessionMCPServerNotEnabled
```

Control — identical call against a **PythonLTS** pool — returns `200 {"apiKey": …}`.

This contradicts Microsoft's own published material:

- The ACA MCP overview states the platform-managed MCP server exposes its tools
  "regardless of the session pool's `containerType`".
- The Foundry custom-code-interpreter article and its official `foundry-samples`
  Bicep provision `containerType: 'CustomContainer'` **together with**
  `mcpServerSettings.isMcpServerEnabled: true`.

Both cannot be true. Either the service is missing support or the docs and sample
are wrong.

**What to do.** If you need MCP today, use a managed `PythonLTS` pool — MCP works
there and is what this POC used for the agent-driven path. If you need custom
packages *and* MCP, keep them separate: run the custom pool via the direct
`/executions` data-plane call (0.08–0.72 s, shown above) and treat MCP as a
managed-pool-only capability until this is resolved. Confirm current status with
your Microsoft account team before committing a design to it.

### 2. API versions: pin deliberately

- `2026-03-02-preview` is **advertised by the resource provider** but is **not
  published** in `azure-rest-api-specs`. It is discoverable via `az provider show`
  — **do not pin it**, as you may be unable to get support for it.
- `mcpServerSettings` is annotated `@removed(Versions.v2026_01_01)`, so **MCP is
  absent from the GA `2026-01-01` contract entirely**. If you standardise on GA,
  you lose the capability silently — pin a preview version where you need MCP.
- Field casing is inconsistent between the spec (`isMcpServerEnabled`), the
  Foundry tutorial (`isMCPServerEnabled`), and an open specs PR (#42917) that
  cites "ACA backend response body casing does not match".
- Conceptual docs disagree on the data plane: `session-pool.md` shows
  `code/execute` with `2024-02-02-preview`, while the TypeSpec and generated REST
  reference use **`/executions`** with `2025-10-02-preview`. We verified
  `/execute`, `/code/execute` and `/executions` all work on **every** advertised
  api-version, so nothing breaks — but the docs should converge.

### 3. `readySessionInstances` is silently ignored on PythonLTS pools

Requested on every API version tried, via CLI and raw ARM. The property simply
never appears in `scaleConfiguration`. **No error, no warning.** It is honoured
on CustomContainer pools.

**What to do.** Don't build a warm-pool strategy on the managed Python pool — you
will get nothing and no signal that it was ignored. Always read the pool back
after deployment and assert the property is present. In practice this matters
less than expected: session start was ~0.36 s, so **concurrency sizing, not
warmth, is the real lever** (see §3 above).

Full detail and traces: [`VALIDATION.md`](VALIDATION.md)

### 4. On-premises connectivity is undocumented end to end — validate before committing

Every building block is documented individually — network injection, peered
VNets, ACA UDR/forced tunneling, Azure Firewall in the agent egress path, DNS
Private Resolver. But nothing states that the **injected data proxy** honors
custom VNet DNS / forwarding rulesets, or that agent OpenAPI tool calls over
ExpressRoute/VPN to on-premises are supported. No documented limitation says
otherwise either.

**What to do.** The building blocks strongly suggest this works — the agent
subnet is an ordinary delegated subnet with no route table restrictions — but we
did not measure it, so treat it as **unverified**. If your internal APIs are
on-premises or in a peered network, prove it with a spike before committing:
deploy the stub API in this repo on the far side of the link and run the same
test. Confirm supportability with your Microsoft account team.

There is also no single authoritative "FQDNs a Foundry prompt agent must reach"
list, which makes locked-down egress a trial-and-error exercise — budget time for
it.

---

## Repository index

| File | Contents |
|---|---|
| [`SECURE-AGENT-GUIDELINES.md`](SECURE-AGENT-GUIDELINES.md) | **Standalone reusable guideline** — secure networking, internal/on-premises API access, latency, code execution. No environment specifics; safe to share as-is |
| [`FINDINGS.md`](FINDINGS.md) | All measured evidence: cold start and PTU (§1), private networking (§2), code execution and multi-language pools (§3), LLM gateway (§4), how to reproduce (§5) |
| [`HOSTED-VS-PROMPT-AGENTS.md`](HOSTED-VS-PROMPT-AGENTS.md) | All six requirements re-measured with **hosted agents** (LangGraph): comparison per requirement, request-context propagation, multi-provider LLM gateway, capability matrix, operational gotchas |
| [`VALIDATION.md`](VALIDATION.md) | Independent re-validation: SDK/API version audit, the two corrected conclusions, PTU measurement |
| [`DEMO-RUNBOOK.md`](DEMO-RUNBOOK.md) | 60-minute session: agenda, commands, talking points, failure responses |
| `track-b/`, `track-c/` | Deployment templates, stub API, and demo runners |
| `probes/` | Measurement harnesses (`revalidate.py`, `ci_verify.py`, `ci_reuse.py`, `ptu_ab.py`) |

## Running the demos

```bash
./preflight.sh          # verifies identity, subscription, and every demo resource
./track-b/run-demo.sh   # ~54s — private internal API access
./track-c/run-demo.sh   # ~96s — code execution comparison
```

`preflight.sh` also guards against the most common failure: the Azure CLI default
subscription silently changing, which makes every resource look deleted.

> **Note on identifiers.** Documentation in this repo is written with redacted,
> generic resource names. The scripts under `track-b/`, `track-c/` and `probes/`
> still reference the live deployment they were built against, and would need
> re-pointing before reuse in another environment.

## Methodology note

One correction is worth calling out for anyone reusing these numbers. The
original code-interpreter benchmark used the prompt
`"Use python to compute sum(range(100))"`. Re-testing proved the model answers
that **from memory and never invokes the tool** — 0 of 6 invocations. Those
timings were partly plain model calls.

Every code-execution measurement in this repo now asserts that a
`code_interpreter_call` actually appears in the response before it is counted.
