# Hosted agents vs prompt agents — measured comparison

The first three sections of [`FINDINGS.md`](FINDINGS.md) answer three customer
requirements using **prompt agents** (agents defined by configuration, executed
by the Foundry-managed runtime). This document repeats the same three
requirements using **hosted agents** — your own container image or code bundle,
running a LangGraph/LangChain harness — deployed into the **same fully
locked-down Foundry account** (`publicNetworkAccess=Disabled`, network
injection into a delegated subnet).

- **Measured:** August 2026
- **Harness:** `langchain-azure-ai` 1.2.8, `ResponsesHostServer`, Responses protocol
- **SDK:** `azure-ai-projects` 2.4.0
- **Model:** `gpt-4o-mini` (GlobalStandard)

Every row is labelled:

- **[Measured]** — observed live in Azure during this POC
- **[Documented]** — from product documentation, not exercised here
- **[Unknown]** — neither documented clearly nor measured; treat as risk

**Measurement caveat.** One environment, one region, one model, small sample
sizes. These show *relative* cost and *where time goes*. Re-measure before
using any figure as an SLA.

**Redaction.** Account, project, subscription and resource names are replaced
with placeholders. Business data (`env-1001`, `Mutual NDA`) is synthetic test
data from a stub API.

---

## Requirement map

DocuSign's stated requirements, and where each is answered.

| Requirement (as stated) | Section | Short answer |
|---|---|---|
| Security — VNet setup and reaching internal DocuSign APIs | [§2](#2-security--vnet-isolation-and-internal-api-access) | Both agent types work privately; hosted needs an explicit RBAC grant |
| Cold start — can main agents be kept alive? | [§3](#3-cold-start-and-keeping-agents-warm) | No issue for prompt agents; hosted pays ~15 s per session, no keep-warm knob |
| Code execution — patterns other tenants use with Container Apps | [§4](#4-code-execution), [§9](#9-multi-language-support) | ACA session pools for untrusted code; in-process only for code you shipped |
| Agent execution & control — control over the agent harness | [§14](#14-choosing-between-them) | This *is* the hosted-vs-prompt choice; hosted gives full harness control |
| LLM gateway — use our existing gateway | [§6](#6-llm-gateway--using-an-existing-multi-provider-gateway) | Yes, and it can be your own non-Azure gateway; it must speak SSE |
| Telemetry & metrics — inject DocuSign custom telemetry | [§7](#7-telemetry-and-metrics) | Hosted only for injection, including to a non-Azure backend; both correlate on trace id |
| IP protection — data, memory and session state | [§11](#11-ip-protection--where-docusign-data-sits) | Customer-owned stores; two gaps to raise (abuse monitoring, hosted source code) |
| Library integration — DSPy and similar | [§8](#8-library-integration--dspy-and-arbitrary-packages) | Hosted only; DSPy 3.3.0 ran a real program in the sandbox |
| Short/long-term memory via filesystem | [§10](#10-memory-filesystem-and-state) | Filesystem is conversation-scoped; long-term needs Foundry Memory or your own store |
| Request context & identity propagation (raised on the call) | [§5](#5-request-context-and-identity-propagation) | Both propagate context; **neither offers generic OBO** |

---

## Contents

1. [Executive summary](#1-executive-summary)
2. [Security — VNet isolation and internal API access](#2-security--vnet-isolation-and-internal-api-access)
3. [Cold start and keeping agents warm](#3-cold-start-and-keeping-agents-warm)
4. [Code execution](#4-code-execution)
5. [Request context and identity propagation](#5-request-context-and-identity-propagation)
6. [LLM gateway — using an existing multi-provider gateway](#6-llm-gateway--using-an-existing-multi-provider-gateway)
7. [Telemetry and metrics](#7-telemetry-and-metrics)
8. [Library integration — DSPy and arbitrary packages](#8-library-integration--dspy-and-arbitrary-packages)
9. [Multi-language support](#9-multi-language-support)
10. [Memory, filesystem and state](#10-memory-filesystem-and-state)
11. [IP protection — where DocuSign data sits](#11-ip-protection--where-docusign-data-sits)
12. [Capability matrix](#12-capability-matrix)
13. [Operational gotchas](#13-operational-gotchas)
14. [Choosing between them](#14-choosing-between-them)
15. [Reproducing](#15-reproducing)

---

# 1. Executive summary

All requirements raised so far are satisfiable in a fully private account.
Several are satisfied *differently* enough between the two agent types to
change an architecture review — and two of them point in opposite directions.

| Requirement | Prompt agent | Hosted agent | Verdict |
|---|---|---|---|
| **1. VNet + internal APIs** | Managed runtime calls the API; connection secret injected by the data proxy | Your code calls the API from its own sandbox; you resolve the secret yourself | Both work. Hosted **needs an explicit RBAC grant** that prompt agents never needed |
| **2. Cold start** | No measurable idle de-allocation penalty | **~15 s to provision every new session**, plus a slow first serving turn | **Materially worse** for hosted |
| **3. Code execution** | Delegate to Code Interpreter or an ACA session pool | Run in-process — ~100,000× faster, but **no isolation** | Different trade, not strictly better |
| **4. Request context propagation** | **`traceparent` + W3C `baggage` reach the tool** (measured); `x-client-*` and `metadata` do not; per-user auth via Toolbox/MCP | **`x-client-*` headers, `metadata` and `traceparent` all measured working** | Hosted is richer, but prompt agents are **not** empty-handed. **Neither offers generic OBO** |
| **5. Existing LLM gateway** | Needs an admin-created **`ModelGateway` connection**; model is `<connection>/<model>` | Just set `base_url` in your own client | Both work. **Gemini is not in the Azure catalog**, so a gateway is the only route to it |
| **6a. Custom telemetry & metrics** | Microsoft's traces only, but **correlated to your trace id** (measured); no custom dimensions or metrics | **Custom spans and metrics measured landing in App Insights *and* in a non-Azure OTLP backend** | Hosted for injection; both for correlation |
| **6b. Library integration (DSPy)** | Not possible in the loop | **DSPy 3.3.0 installed and ran** | Hosted only |
| **6c. Filesystem memory** | n/a | Writable 4.1 GB disk, **persists per conversation, not across them** | Short-term yes, long-term no |
| **6d. Multi-language execution** | Session pools: Node, Shell, C#, GPU, custom | Same pools; runtime itself is Python | Both, via pools |

**The five findings most worth raising in a design review:**

1. **Hosted agents ship with zero RBAC.** The platform creates two Entra
   identities per agent and grants them **nothing**. Anything the agent needs —
   reading a project connection, Cosmos, Key Vault — is a manual grant. Prompt
   agents never surface this because the managed data proxy holds the
   credentials. §2.3.
2. **Hosted agents have a real cold start; prompt agents effectively do not.**
   Session provisioning measured 13.7–16.7 s across seven runs, and `ACTIVE`
   status does **not** mean ready to serve — the first real turn took 45–97 s
   and sometimes exceeded a 120 s timeout. §3.
3. **The agent sandbox has unrestricted outbound internet**, even though the
   Foundry account itself is `publicNetworkAccess=Disabled`. Code executed
   in-process can also read every environment variable and mint managed-identity
   tokens. For a customer whose driver is data isolation, in-process execution
   of model-generated code is a data-exfiltration path. §4.2.
4. **Google Gemini is not in the Azure model catalog.** Eleven publishers are
   offered in `eastus2` — Anthropic, Meta, Mistral, DeepSeek, xAI and others —
   but **no Google**. Every other named provider can be used natively without a
   gateway; Gemini can only be reached *through* one. §6.2.
5. **The new requirements split the decision.** Custom telemetry from inside
   the agent, DSPy, and filesystem working memory are **hosted-agent-only**
   capabilities. But enforcing the LLM gateway as a governed control point is
   **prompt-agent-only**, because a hosted agent can point `base_url` anywhere.
   These pull in opposite directions and the resolution is a design decision,
   not a measurement. §14.1.

---

# 2. Security — VNet isolation and internal API access

> *Requirement: "the VNet setup and accessing DocuSign internal APIs using the
> agent service."*

**Both models reach a private, VNet-only internal API. The difference is who
holds the credential and who must be granted access.**

## 2.1 Result

| | Prompt agent | Hosted agent |
|---|---|---|
| API reached over private network | Yes **[Measured]** | Yes **[Measured]** |
| Correct business data returned | `env-1001` / `Mutual NDA` **[Measured]** | `env-1001` / `Mutual NDA` **[Measured]** |
| Who calls the API | Foundry-managed data proxy in the injected subnet | Your agent process in the managed sandbox |
| Who holds the API key | Data proxy, from a project connection | Your code, fetched at runtime from the project connection |
| Key present in image or env | No | No — resolved per call via managed identity **[Measured]** |
| Extra RBAC required | None | **Yes — `Foundry User` on the project** **[Measured]** |

Hosted-agent tool output, verbatim, after the grant:

```text
TOOL_CALL name=get_envelope_status
TOOL_OUTPUT={'envelope_id': 'env-1001', 'status': 'completed',
             'document_name': 'Mutual NDA', 'recipient': 'alex@example.internal',
             'updated_at': '2026-08-04T18:30:00Z',
             '_credential_source': 'project_connection'}
```

`_credential_source` is emitted by the tool itself, so the evidence is that the
key came from the project connection — not that the model said so.

## 2.2 Deployment is a data-plane operation

Deploying a hosted agent goes through the **project data plane**, not ARM.
With `publicNetworkAccess=Disabled` this changes CI/CD design:

| Where the deploy runs | Result | Time | Label |
|---|---|---:|---|
| Engineer laptop / public build agent | `403 Public access is disabled` | 2.21 s | **[Measured]** |
| Container job inside the delegated VNet | `DEPLOY_ACCEPTED` | 30.21 s first, 4.01 s later | **[Measured]** |

ARM does not expose agents at all — `.../agents` returns `UnsupportedAction`.
Therefore **deploy, list, inspect and invoke all require in-VNet execution**.

> **Consequence.** `azd up` from a laptop or a stock GitHub/ADO hosted runner
> will not work against a locked-down account. The pipeline needs a
> VNet-injected self-hosted runner or a container job, exactly like the
> in-VNet job used throughout this POC.

Prompt agents are affected by the same rule for *invocation*, but their
definition is configuration rather than a code artifact, so there is no build
and push step to relocate.

## 2.3 The RBAC gap — hosted agent identities start with nothing

Each hosted agent version gets **two** Entra identities:

```text
blueprint.principal_id  = <guid>   # ...-AgentIdentityBlueprint  (type: Application)
instance_identity.principal_id = <guid>   # ...-AgentIdentity     (type: ServiceIdentity)
```

`az role assignment list` returned **empty for both** **[Measured]**. The first
attempt to read the project connection therefore failed, and the tool reported
it precisely:

```text
{'error': 'HTTP 401', 'detail': 'Invalid API key',
 '_credential_source': 'project_connection_error:ClientAuthenticationError'}
```

Note the shape of that failure: the agent **reached the private API** — a
network success — and was rejected on **credentials**. Granting `Foundry User`
to the *instance* identity on the project scope fixed it, unchanged code.

> **Design point.** For hosted agents the agent identity is a first-class
> security principal you must provision, review and rotate access for. Budget
> for it in the landing zone. Prompt agents delegate this to the managed proxy,
> which is less flexible but also less to get wrong.

## 2.4 Egress is *not* restricted by the account's private setting

From inside the agent sandbox **[Measured]**:

```text
egress: {'public_internet': 'reachable:200', 'public_internet_ms': 42.0,
         'azure_control_plane': 'reachable:400', 'azure_control_plane_ms': 43.7}
```

`publicNetworkAccess=Disabled` controls **inbound** access to the Foundry
account. It says nothing about **outbound** access from your agent container.
If egress control is a requirement, it must be imposed on the subnet — NSG,
UDR, Azure Firewall or a proxy — and verified, not assumed.

This mirrors the prompt-agent result in `FINDINGS.md` §3.1, where egress from
the ACA code-execution sandbox was **blocked** because the pool was explicitly
configured `EgressDisabled`. The control existed and was switched on. For
hosted agents there is no equivalent pool-level switch — it is subnet policy or
nothing.

---

# 3. Cold start and keeping agents warm

> *Requirement: "documentation indicates a 5 to 10-second latency due to agent
> de-allocation. Is there a way to keep a few main agents always alive?"*

**This is where the two models diverge most, and not in the hosted agent's
favour.**

`FINDINGS.md` §1 establishes that for prompt agents, the "5–10 second
de-allocation latency" concern does not reproduce: the model layer shows a
~1.3 s idle delta, the managed agent runtime shows no measurable penalty and
exposes no warm-up control because it needs none, and the code sandbox costs
~0.36 s.

Hosted agents introduce a layer prompt agents do not have: **your container,
started per session.**

## 3.1 Measured latencies

| Phase | Measurement | Label |
|---|---:|---|
| `create_session` → status `ACTIVE` | **13.74 – 16.70 s** (7 runs, mean ~15.4 s) | **[Measured]** |
| Same, after 19 minutes idle | **16.70 s** — no worse than warm | **[Measured]** |
| First serving turn after `ACTIVE` | **45 – 97 s**, sometimes >120 s timeout | **[Measured]** |
| Steady-state turn, conversation pinned | **8.80 – 10.77 s** | **[Measured]** |
| Prompt agent, equivalent runtime penalty | none measurable | **[Measured]**, `FINDINGS.md` §1.1 |

## 3.2 `ACTIVE` does not mean ready

The most operationally dangerous result. `get_session` reports `ACTIVE` about
15 s in, but a request sent then can still block for a minute or more while the
container finishes starting. A health check that trusts `ACTIVE` will route
traffic into a stall.

Representative run, on a cleaned-up agent:

```text
TIME_TO_ACTIVE_S=13.98
TURN=1 STATUS=200 LATENCY_S=45.16
TURN=2 STATUS=-1  LATENCY_S=120.12     # client timeout
TURN=3 STATUS=200 LATENCY_S=47.02
TURN=4 STATUS=200 LATENCY_S=8.80
TURN=5 STATUS=200 LATENCY_S=8.81
TURN=6 STATUS=200 LATENCY_S=10.77
```

Turns 1–3 each landed on a fresh session (see §3.3). From turn 4 the
conversation was pinned and latency settled into single digits.

## 3.3 Every unpinned request mints a new session

On the Responses protocol, a POST that does not continue an existing
conversation **creates a new agent session**. Sessions then linger (`IDLE`, with
a rolling 30-day expiry). This POC accumulated **160** of them **[Measured]**.

Two consequences:

- **Latency.** An unpinned request can pay session provisioning every time.
- **Measurement validity.** A naive retry loop measures nothing useful, because
  each retry is a different session.

Continuation on the Responses protocol uses **`previous_response_id`** (or
`conversation.id`). The `agent_session_id` query parameter belongs to the
**Invocations** protocol; passing it to a Responses endpoint made requests hang
until the client timed out **[Measured]**. The samples show both protocols side
by side, which makes this easy to get wrong.

## 3.4 Answering "can we keep a few main agents always alive?"

| | Prompt agent | Hosted agent |
|---|---|---|
| Is there an idle-deallocation problem? | No **[Measured]** | Yes, per session **[Measured]** |
| Platform keep-warm setting | None, none needed | **None exposed** **[Measured]** |
| Practical mitigation | Not required | **Pin conversations** and keep a synthetic heartbeat per pinned session |

No `minReplicas` or `readySessionInstances` equivalent was found for hosted
agents in SDK 2.4.0 **[Measured — absence of API]**. The workable pattern is
application-level: maintain a pool of long-lived conversations, keep them alive
with periodic cheap turns, and route users onto an already-warm one. That is
work the prompt-agent model does not require.

> **Honest caveat.** These numbers come from a preview surface in one region
> with a small sample, and the first-turn figures were noisy (including two
> outright timeouts). The *shape* — a real per-session start cost, and `ACTIVE`
> preceding readiness — reproduced consistently. The exact seconds should not be
> quoted as an SLA.

---

# 4. Code execution

> *Requirement: "code execution … and patterns other tenants are using for
> Azure Container Apps in this space."*

## 4.1 Measured comparison

| Pattern | Model | Execution time | Isolation | Label |
|---|---|---:|---|---|
| In-process `exec` in the agent | Hosted | **0.15 ms** | **None** — shares agent identity, env, network | **[Measured]** |
| Custom container ACA session, direct | Prompt | 0.57 s | Strong; egress provably blocked | **[Measured]** `FINDINGS.md` §3.1 |
| Managed pool via MCP agent | Prompt | 2.85 s | Strong | **[Measured]** `FINDINGS.md` §3.1 |
| Built-in Code Interpreter | Prompt | 5.94 s median | Managed | **[Measured]** `FINDINGS.md` §3.1 |

Hosted in-process execution is roughly **four orders of magnitude** faster than
the nearest sandboxed option. The reason is that there is nothing to provision:
the agent is already a process.

```text
{'execution_mode': 'in_process_hosted_container',
 'stdout': '333833500\n', 'elapsed_ms': 0.15}
```

## 4.2 The isolation caveat

That speed is not free. Probing the execution context from inside the agent
**[Measured]**:

```text
hostname: adc-sandbox    pid: 36    env_var_count: 49
managed_identity_reachable_from_exec: true
egress: public_internet reachable:200
env vars visible to executed code include:
  IDENTITY_ENDPOINT, IDENTITY_HEADER, FOUNDRY_PROJECT_ENDPOINT,
  FOUNDRY_AGENT_TOOLSET_ENDPOINT, COSMOS_ENDPOINT, INTERNAL_API_BASE, ...
```

Code executed in-process can mint managed-identity tokens, read every
configured endpoint, and reach the public internet. **If the code being
executed is generated by a model or influenced by user input, in-process
execution is a privilege-escalation and exfiltration path.**

The hosted agent runs in a Microsoft-managed sandbox (`adc-sandbox`), which
isolates it from *other tenants*. It does not isolate executed code from *your
own agent's* credentials.

> **Recommendation.** Use in-process execution only for code you wrote and
> shipped in the image. For model-generated or user-supplied code, a hosted
> agent should call out to an ACA dynamic session pool exactly as a prompt agent
> does — the patterns in `FINDINGS.md` §3.2–3.4 apply unchanged, and the
> `EgressDisabled` + `CustomContainer` configuration remains the way to make
> package versions and egress provable.

Hosted agents therefore do not remove the need for the ACA session-pool pattern;
they add a fast path that is only appropriate for trusted code.

---

# 5. Request context and identity propagation

> *"Can hosted agents and prompt agents receive, store, and propagate
> customer-defined request context (identity, tenant, correlation IDs) to
> downstream tools and APIs, and what portion of that context can be
> represented through OBO versus custom metadata/header propagation?"*

**Short answer.** Hosted agents have a real, working context channel — measured
end to end. Prompt agents have a **narrower but genuine** one: caller-supplied
W3C `traceparent` and `baggage` were measured arriving at a downstream API,
though `x-client-*` headers and `metadata` were not (§5.4.1). **Neither type
gives you generic OBO to an arbitrary internal API**: the caller's
`Authorization` header is deliberately never delivered to agent code. And
because every one of these channels is caller-asserted, none of them is an
authorization mechanism — see §5.7 for the distinction that decides the design.

## 5.1 What actually arrives

A hosted agent was called with caller-supplied headers, a `metadata` object and
a W3C `traceparent`. It echoed back everything the platform gave it
**[Measured]**:

```text
client_headers: {'x-client-tenant-id': 'contoso-eu',
                 'x-client-correlation-id': 'corr-12345',
                 'x-client-end-user': 'alex@example.internal'}
request_metadata: {'tenant': 'contoso-eu', 'request_id': 'req-999'}
platform_user_id: <object-id of the calling principal>
platform_call_id: proxy_90bf35b744e2290900uP7AV0Mgj...
otel_span: {'trace_id': '4bf92f3577b34da6a3ce929d0e0e4736', ...}
```

Four things are established by that single result:

| Sent by caller | Reached agent code? | Label |
|---|---|---|
| `x-client-tenant-id`, `x-client-correlation-id`, `x-client-end-user` | **Yes** | [Measured] |
| `x-custom-not-prefixed` (no `x-client-` prefix) | **No — silently dropped** | [Measured] |
| Responses `metadata` object | **Yes**, verbatim | [Measured] |
| `traceparent` | **Yes** — the agent's OTel span carried the *same* trace id `4bf92f35…4736` | [Measured] |

The trace-id match is exact, so **W3C trace context genuinely propagates into
the agent** rather than a new trace being started. Correlation across a
distributed system therefore works without any custom plumbing.

## 5.2 The allowlist is real, and `Authorization` never arrives

Only the `x-client-` prefix passes. This is by design in the AgentServer wire
contract (`azure.ai.agentserver.core.platform_headers`), and the measurement
above confirms the ingress enforces it. Also never forwarded: `Authorization`,
`Cookie`, `Host`, `x-forwarded-*` **[Documented]**.

> **This is the crux of the OBO question.** Because hosted agent code never
> receives the caller's bearer token, it cannot perform a standard Entra
> on-behalf-of exchange with it. OBO to an arbitrary internal API is not
> available by simply "passing the token through".

## 5.3 Two platform identity signals, and a trap

The platform injects two values **[Measured]**:

- **`x-agent-user-id`** — a stable, cross-agent identifier for the **calling
  principal**. Use it to partition per-user state.
- **`x-agent-foundry-call-id`** — an opaque per-request call id. The container
  **must forward it verbatim** on outbound calls to Foundry services (Toolbox/MCP,
  Storage, A2A) so those services can resolve caller context server-side. Never
  parse it **[Documented]**.

> **The trap.** Here, `x-agent-user-id` came back as the object id of the
> **service identity that made the call**, not a human. If a web tier calls the
> agent with its own managed identity — the normal enterprise pattern —
> `x-agent-user-id` identifies **your application**, not the end user. It is not
> an end-user identity unless end users authenticate to the agent endpoint
> directly.

The forwarding set a tool would send downstream, measured:

```text
{'x-client-tenant-id': 'contoso-eu',
 'x-client-correlation-id': 'corr-12345',
 'x-agent-foundry-call-id': 'proxy_9a787f2b66e89da600...'}
```

Note what this means: propagation to **your own** downstream APIs is something
**your code does explicitly**. Nothing is auto-injected into arbitrary outbound
HTTP calls.

## 5.4 Prompt agents

| Question | Answer | Label |
|---|---|---|
| Per-request custom headers into tool calls | **No** — `x-client-*` is dropped before the tool call | [Measured] |
| Per-request custom context into tool calls | **Yes, via W3C `baggage`** — caller-supplied entries arrive | [Measured] |
| Conversation/thread metadata | Yes: ≤16 pairs, key ≤64, value ≤512 chars | [Documented] |
| Metadata auto-mapped into OpenAPI tool headers | **No** | [Documented — absence] |
| Direct OpenAPI tool auth options | anonymous, connection (key/token), managed identity — **no user-token option** | [Documented] |
| Per-user auth via MCP/Toolbox connections | **Yes** — `oauth2` and `user-entra-token` | [Documented] |
| W3C trace context into every tool call | **Yes** — caller's trace id arrives at the API | [Measured] |

So a prompt agent *can* act with a **specific user's** delegated permissions —
but only through **Toolbox/MCP connections**, where Foundry manages consent,
storage, refresh and injection.

### 5.4.1 What reaches a downstream API — measured on the wire

A prompt agent was given an OpenAPI tool pointing at an echo API
(`track-b/context-echo/`) that records every inbound header, then invoked with
caller-supplied headers, `metadata`, a `traceparent` and a `baggage` header.
**The API records the wire, so the model cannot flatter the result.**

| Sent by caller | Reached the internal API? | Label |
|---|---|---|
| `x-client-tenant-id`, `x-client-correlation-id`, `x-client-end-user` | **No** | [Measured] |
| Responses `metadata` object | **No** — not as headers, not as query | [Measured] |
| `traceparent` | **Yes** — same trace id, new span id | [Measured] |
| **`baggage`** | **Yes** — caller entries arrive, merged with the platform's | [Measured] |

The exact bytes the API received:

```text
traceparent: 00-11112222333344445555666677778888-909d89b9288336d0-01
baggage:     docusign_tenant = contoso-eu, docusign_corr = corr-prompt-77,
             leaf_customer_span_id = 8e6fbb6cd61dad1f
```

The trace id is **byte-identical** to the one the caller sent, and the span id
is a new child — correct W3C behaviour. So end-to-end correlation from your web
tier, through the prompt agent, into your internal API works today with no
custom plumbing.

**`baggage` is the practical channel.** It is the W3C standard
carrier for arbitrary key/value request context, and caller-supplied entries
survive the whole path. Prompt agents therefore *do* have a per-request custom
context channel — just not the `x-client-*` one hosted agents use.

Four caveats, all of which matter before anyone builds on it:

* **It is caller-asserted, so it is not authorization.** Identical rule to
  `x-client-*` (§5.5). Fine for tenant routing, correlation, locale, feature
  flags. Never for entitlement decisions.
* **Oversized entries are silently dropped.** A ~2 KB baggage header arrived
  intact; at ~8 KB the large entry vanished with **no error** while the small
  entries survived. Do not put tokens or documents in it, and alert on absence
  rather than assuming delivery. [Measured]
* **Whitespace is re-serialized** — values came back as `key = value` with
  spaces around the `=`. Parse tolerantly; use a real baggage library.
* **This is not a documented product feature.** It is an observable consequence
  of the platform's OpenTelemetry instrumentation, so it could change without
  notice. Worth confirming as a supported contract with the product team before
  it becomes load-bearing.

**The model-mediated alternative, also measured.** Instructing the agent to pass
`correlation_id` as a tool *parameter* worked — the value arrived as a query
string. But the model decides whether to comply, the value is visible in
conversation state, and it is reachable by prompt injection. Use it for
convenience data, never for anything that matters.

Passing context through the prompt is **not** a propagation mechanism: it has no
integrity, is visible to the model, and is subject to prompt injection. Never use
it for anything security-relevant.

## 5.5 On OBO specifically — what "Agent ID" does and does not mean

**Microsoft Entra Agent ID is an identity *for the agent*, not a carrier of the
user's identity** **[Documented]**. It supports two token shapes: autonomous
(subject = agent) and delegated (subject = user, **actor** = agent). Entra does
document an agent OBO flow, where a blueprint identity exchanges a user
assertion for a downstream token.

The gap is delivery: **Foundry does not hand your agent a user assertion.**
Therefore, of the customer's context list:

| Context | Representable via OBO | Representable via metadata/headers |
|---|---|---|
| **Correlation / trace ids** | n/a | **Yes** — `x-client-*` and `traceparent` [Measured] |
| **Tenant id** | Indirectly, as a token claim | **Yes**, but **untrusted** — it is caller-asserted |
| **End-user identity** | **Only** via Toolbox `oauth2` / `user-entra-token`, or your own broker | Only as an **unverified claim** |

**Rule of thumb:** headers and metadata are fine for *routing, logging and
correlation*. They are **not** an authorization mechanism, because anything the
caller asserts, the caller can forge.

## 5.6 Recommended patterns

Where the platform stops, these fill the gap. Ordered by preference.

**1. Toolbox / MCP connection auth — use this first where it fits.**
Configure the connection as `oauth2` or `user-entra-token`. Foundry handles
consent, token storage, refresh and injection, for both agent types. Nothing
sensitive touches model-visible state.

**2. Token-exchange broker for custom internal APIs.**
A small confidential-client service that the agent calls with its own managed
identity plus the `x-agent-foundry-call-id`/session context. The broker
authenticates the *caller*, looks up the authoritative user binding, performs the
Entra OBO exchange, and either returns a **narrowly scoped, short-lived** token
or — better — performs the downstream call itself. This keeps user credentials
out of the agent process entirely.

**3. Server-side context store keyed by session and platform user id.**
Your front door writes authoritative context (real end-user id, tenant,
entitlements) into a store keyed by `(agent_session_id, x-agent-user-id)` before
invoking the agent; the agent reads it. Because the agent never trusts a
caller-asserted value, forgery is not possible. This POC already demonstrated the
storage half — private-endpoint Cosmos, keyless, ~745 ms write (§10.4).

**4. `x-client-*` headers (hosted) or W3C `baggage` (either type) for
non-authoritative context.**
Correlation ids, locale, feature flags, request tags. Cheap, measured to work.
`baggage` is the only per-request custom channel that reaches a **prompt
agent's** tools (§5.4.1); `x-client-*` does not survive that hop. Keep entries
small, validate on arrival, and never authorize on either.

**5. Egress gateway or sidecar** for deterministic header injection, destination
allowlisting and audit — useful given that agent-sandbox egress is otherwise
unrestricted (§2.4).

**Explicitly do not:** pass an end-user bearer token as a tool parameter or in
model-visible content. It leaks into traces, logs, retries and conversation
history, and is reachable by any code the agent executes (§4.2).

## 5.7 Reference architecture

```text
end user ──auth──> your web/API tier ──────────────────────────────┐
                        │ (validates user, mints correlation id)    │
                        │                                           │
                        ├─ writes authoritative context ──> context store
                        │     key: (agent_session_id, user)         │
                        │                                           ▼
                        └─ POST /agents/{name}/endpoint/...  Foundry ingress
                             x-client-correlation-id: ...      (strips Authorization,
                             traceparent: ...                   keeps x-client-*,
                             metadata: {...}                    injects call id + user id)
                                                                   │
                                                                   ▼
                                                            hosted agent code
                                                    reads client_headers + context store
                                                                   │
                              ┌────────────────────────────────────┼───────────────┐
                              ▼                                    ▼               ▼
                    Toolbox / MCP tool                    token broker        internal API
               (per-user oauth2 / entra token)        (Entra OBO exchange)  (correlation hdrs)
                    forward x-agent-foundry-call-id
```

**Split of responsibilities.** Correlation and tracing: the platform, measured
working. Authoritative identity and tenant: your context store or broker. Never
the prompt.

### 5.7.1 Why a context store, when the headers demonstrably work?

A fair objection: §5.1 measured `x-client-*`, `metadata` and `traceparent`
arriving intact, so why does the diagram add a store?

**Because delivery and trust are different problems, and only delivery was
measured.** The headers arrive reliably — that is settled. But they arrive
*exactly as the caller wrote them*, and the platform performs no validation of
their contents. Anything a caller asserts, a caller can forge. `x-client-end-user:
alex@example.internal` is a string, not a proof.

So the split is:

| Context | Channel | Why |
|---|---|---|
| Correlation id, trace id, locale, feature flags | **Headers / baggage** | Forging them harms only the forger |
| End-user identity, tenant, entitlements, roles | **Context store or broker** | An authorization decision depends on it |

The store is keyed by `(agent_session_id, x-agent-user-id)` precisely so the
agent never has to trust a value the caller supplied: the front door writes the
authoritative record *after* it has authenticated the user, and the agent reads
it back using platform-injected identifiers it did not receive from the caller.

**When you can skip it.** This is a threat-model decision, not a rule. If the
agent endpoint is reachable by exactly one caller — your own front door, over a
private endpoint, holding the only principal with invoke rights — then
"caller-asserted" and "trusted" collapse into the same thing, and headers alone
are defensible. Write down that assumption, because it breaks the moment a
second team is granted access to the project, or end users are allowed to call
the agent directly.

**The second reason is size.** Headers and baggage are small: `metadata` is
capped at 16 pairs of ≤512 chars, and oversized baggage entries are dropped
silently (§5.4.1). An entitlements list or a document-scope set does not fit. A
store has no such ceiling, and the agent fetches only what it needs.

### 5.7.2 The prompt-agent path

The diagram above is the hosted-agent shape. For a prompt agent, Microsoft owns
the loop, so there is no place to put your code between the ingress and the tool
call. What survives is narrower:

```text
your web/API tier ──> Foundry ingress ──> prompt agent (Microsoft's loop)
   traceparent: ...                                  │
   baggage: tenant=...,corr=...                      │ platform forwards
   (x-client-* and metadata stop here)               │ traceparent + baggage
                                                     ▼
                            ┌────────────────────────┴──────────────┐
                            ▼                                       ▼
                   OpenAPI tool ──> internal API          Toolbox / MCP tool
              (reads traceparent + baggage;              (per-user oauth2 /
               resolves authoritative context             user-entra-token —
               from the store, keyed by tenant            Foundry injects the
               + correlation id)                          token, not you)
```

Two practical routes for "get DocuSign context into an internal API":

1. **Correlation-only, then resolve server-side.** Put a correlation id in
   `baggage`, and have the internal API — or a thin facade in front of it — look
   the authoritative context up from the same store the front door wrote to.
   This is the §5.7.1 pattern with the *API*, rather than the agent, doing the
   lookup, and it is the closest prompt-agent equivalent of the hosted design.
2. **Per-tenant connections.** Bind identity to the tool configuration instead of
   the request: a separate connection (and agent version) per tenant, so the
   downstream credential is inherently scoped and nothing needs to travel
   per-request. Costs you N agents, buys you no trust problem.

For **per-user** authorization, neither route applies — use Toolbox/MCP
connection auth (pattern 1 in §5.6), which is the only mechanism where Foundry
itself vouches for the user.

---

# 6. LLM gateway — using an existing multi-provider gateway

> *Requirement: "we need the ability to utilise our existing LLM gateway."*

**The customer's gateway exists to reach several providers — Azure OpenAI and
Google Gemini were named explicitly — through one governed endpoint.**

Both agent types can use it, but by completely different mechanisms, and only
one of them is a supported platform feature rather than "your code can do
whatever it likes".

## 6.1 Result

| Question | Prompt agent | Hosted agent | Label |
|---|---|---|---|
| Use a non-OpenAI model that Foundry hosts natively (Grok, Llama, DeepSeek, Mistral) | **Yes**, incl. tool calling | Yes | [Measured] |
| Use **Google Gemini** natively from the Azure catalog | **No — not in the catalog** | No | [Measured] |
| Point `model` at a raw gateway URL | **No** — `invalid_engine_error` | n/a | [Measured] |
| Use a gateway via a **`ModelGateway` connection** (BYOM) | **Yes** — `<connection>/<model>` | n/a | [Measured] |
| Tool calling survives the gateway hop | **Yes** | Yes | [Measured] |
| Point the model client straight at the gateway from code | No | **Yes**, no connection or admin needed | [Measured] |
| Must the gateway be Azure API Management / an Azure product | **No** — any OpenAI-compatible endpoint | No | [Measured] |
| Can the gateway be private (no public exposure) | **Yes** | Yes | [Measured] |

**Bottom line:** the requirement is satisfiable for both agent types, and the
gateway can be **the customer's own**. Prompt agents need an admin-created
`ModelGateway` connection and a gateway that meets a real technical contract
(§6.6); hosted agents just set `base_url`.

## 6.2 Gemini is not in the Azure catalog

Enumerating every model offered in `eastus2` returns **11 publishers**:

```
Anthropic  Mistral AI  DeepSeek  Meta  Cohere
Microsoft  xAI  Black Forest Labs  MoonshotAI  Alibaba  OpenAI
```

Searching for publisher `Google` or any model whose name contains `gemini`
returns **zero rows**. [Measured]

So "multi-provider" splits into two very different problems:

* **Anthropic, Meta, Mistral, DeepSeek, xAI, Cohere** — already first-class
  Azure models. No gateway needed at all. Deploy them and point an agent at
  them.
* **Google Gemini** — no Azure path exists. A gateway is the *only* way to
  reach it from a Foundry agent.

Two deployment frictions worth knowing before planning:

* **xAI Grok deployed cleanly** into the locked-down account. [Measured]
* **Anthropic did not**, failing with `InvalidModelProviderData`: Claude
  deployments require `industry`, `organizationName` and `countryCode`
  registration data. Budget for a commercial/legal step, not just an ARM call.
  [Measured]

## 6.3 Prompt agents on a non-OpenAI model

Deployed `grok-4-1-fast-non-reasoning` and gave a prompt agent a function tool:

| Model | Run status | Tool call emitted | Time |
|---|---|---|---|
| `grok-fast` (xAI) | `requires_action` | ✅ `get_envelope_status` | 8.58 s |
| `gpt-4o-mini` (baseline) | `requires_action` | ✅ `get_envelope_status` | 5.07 s |

Tool calling is the thing that usually breaks on non-OpenAI models, and it
worked. [Measured]

> Do **not** generalise this. Foundry publishes a per-model tool-support matrix
> and it is uneven — Cohere Command R and several Mistral models are documented
> as supporting only Code Interpreter and File Search, with **no** Functions,
> MCP or OpenAPI. Check the matrix per model, not per provider. [Documented]

## 6.4 What does *not* work: putting a URL in `model`

The obvious approach fails, and it fails late:

| `model` value | Create | Run |
|---|---|---|
| `https://my-gateway.internal/v1/chat/completions` | **200 OK** | ❌ `invalid_engine_error` |
| `gemini-2.5-pro` | **200 OK** | ❌ `invalid_engine_error` |

> `Failed to resolve model info for: <value>`

**Agent creation does not validate `model` at all.** A typo or an unsupported
model is only discovered on the first run. Validate by running one turn in CI,
never by checking that creation returned 200. [Measured]

## 6.5 What does work: a `ModelGateway` connection (BYOM)

Foundry's supported answer is **bring your own model**: an admin registers the
gateway as a connection, declares which models it exposes, and agents then
reference them as `<connection-name>/<model-name>`.

This POC deployed a real OpenAI-compatible gateway (`track-d/gateway/gateway.py`) as a Container App
**inside the VNet**, routing by model name:

* `gemini-*` → a stub standing in for Google Gemini, representing a provider
  Azure has no catalog entry for
* `gpt-*` → the **real Azure OpenAI deployment**, keyless via managed identity

Results, from a v2 prompt agent in the locked-down project:

| Test | Model | Outcome | Time |
|---|---|---|---|
| Non-Azure provider | `poc-llm-gateway/gemini-2.5-pro` | ✅ gateway's answer returned to the agent | 2.08 s |
| Real Azure OpenAI via gateway | `poc-llm-gateway/gpt-4o-mini` | ✅ returned `GATEWAY_OK` | 6.18 s |
| Tool calling via gateway | `poc-llm-gateway/gemini-2.5-pro` | ✅ `function_call get_envelope_status` | 0.73 s |

The gateway's own request log confirms the traffic transited it. [Measured]

### Three traps

**1. Foundry always requests streaming — the most costly one to miss.**
Every request Foundry sent the gateway contained `"stream": true`:

```json
{"messages": [...], "stream": true, "max_completion_tokens": 16384,
 "stream_options": {"include_usage": true}, "model": "gpt-4o-mini", ...}
```

A gateway that answers with a perfectly valid **non-streaming** OpenAI JSON
body gets the request retried three times and then surfaces to the caller as an
opaque `500 server_error` — with nothing in the message pointing at streaming.
The gateway must emit **SSE `chat.completion.chunk` events terminated by
`data: [DONE]`**, including `usage` on the final chunk when
`stream_options.include_usage` is set. **Verify this before anything else.**
[Measured]

**2. Connection metadata must be a JSON *string*.**
`metadata.models` is a map of string→string. Passing a real JSON array is
rejected with *"unable to deserialize request body"*; it has to be
`json.dumps(...)` into a string. Connection creation can also return
`InternalServerError` several times in a row with an unchanged payload —
**retry before assuming the payload is wrong.** [Measured]

**3. Container Apps has no IMDS.**
The gateway's managed-identity call to `169.254.169.254` was refused instantly.
ACA injects `IDENTITY_ENDPOINT` / `IDENTITY_HEADER` instead. It presents as a
fast `502` that looks like a network policy problem and is not. [Measured]

### What Foundry tells your gateway

Every BYOM request carried:

| Header | Example | Use |
|---|---|---|
| `x-ms-foundry-agent-id` | `gwtest-b1-gemini:1` | Per-agent quota, routing, chargeback |
| `x-ms-foundry-model-id` | `poc-llm-gateway/gemini-2.5-pro` | Requested connection/model |
| `x-ms-foundry-project-id` | project GUID | Per-project attribution |
| `x-ms-client-request-id` | GUID | Correlation with Foundry's request id |

This is genuinely useful: the gateway can enforce policy and bill per agent and
per project without any cooperation from the agent author. [Measured]

Static `customHeaders` can also be attached to the connection for routing
policy. [Documented]

## 6.6 What the gateway has to be, and what it has to do

**It does not have to be an Azure product.** The `ModelGateway` connection
takes a URL. The gateway measured here is a ~300-line Python `http.server`
(`track-d/gateway/gateway.py`) deployed as a Container App — no API Management,
no Azure AI Gateway, no Azure LLM product anywhere in the path. Azure API
Management is a *documented, supported* option with extra features (token
limits, semantic caching, managed-identity validation), not a requirement.
[Measured]

The connection that worked:

| Field | Value |
|---|---|
| `category` | `ModelGateway` |
| `authType` | `ApiKey` (project managed identity also supported [Documented]) |
| `target` | base URL ending in `/v1` |
| `metadata.models` | **JSON-encoded string** listing the exposed models |
| `metadata.deploymentInPath` | whether the model name goes in the URL path |

### Requirements checklist for a customer-built gateway

| # | Requirement | Label |
|---|---|---|
| 1 | Serve **OpenAI Chat Completions** at `{target}/chat/completions` | [Measured] |
| 2 | **Support SSE streaming** — Foundry always sends `"stream": true` | [Measured] |
| 3 | Emit `chat.completion.chunk` events ending in `data: [DONE]` | [Measured] |
| 4 | Include `usage` on the final chunk when `stream_options.include_usage` is set | [Measured] |
| 5 | Tolerate `max_completion_tokens` (Foundry sends `16384`) | [Measured] |
| 6 | Relay `tools` / `tool_choice` and return `tool_calls` unchanged | [Measured] |
| 7 | Accept the connection's credential (API key header, or validate the Entra token) | [Measured] |
| 8 | Be reachable from the Foundry project — **private is fine, see below** | [Measured] |
| 9 | Return HTTP 200 with an SSE body; non-2xx is retried 3× then surfaces as an opaque 500 | [Measured] |

Requirement 2 is the most common failure. A gateway that answers with a
perfectly valid **non-streaming** OpenAI JSON body is not "slightly wrong" —
it fails, silently and unhelpfully, as described in §6.5.

### The gateway can live entirely inside your network

The gateway used here sat on a VNet-injected Container Apps environment, so its
hostname resolved to a **private IP**. It was unreachable from the engineer's
laptop — every test had to be driven from inside the VNet — and **Foundry
reached it anyway**. [Measured]

That matters for DocuSign: the gateway, and therefore the provider credentials
it holds, never has to be exposed publicly.

### What it does *not* have to do

* It does not have to implement the **Responses** API — Chat Completions is
  what Foundry's BYOM path calls. [Measured]
* It does not have to host models itself; it can be a pure router.
* It does not have to be in the same subscription or region as Foundry.
* `/v1/models` is not required for a working run (a static `metadata.models`
  list is what Foundry reads), though it is useful for your own tooling.
  [Measured]

## 6.7 Hosted agents: just set `base_url`

A hosted agent owns its model client, so no connection, admin step or platform
feature is involved:

```python
ChatOpenAI(
    model="gemini-2.5-pro",
    base_url=GATEWAY_BASE_URL,     # the customer's gateway
    api_key=GATEWAY_API_KEY,
    use_responses_api=False,       # the gateway speaks Chat Completions
)
```

Measured from a hosted agent in the locked-down project: the call reached the
gateway in **39.2 ms** and returned the gateway's content, with the gateway's
own log recording it as a **non-streaming** request — the distinguishing
fingerprint against Foundry's always-streaming BYOM calls. [Measured]

Three consequences:

* The gateway does **not** have to support SSE for this path, because your code
  chooses whether to stream.
* Nothing constrains you to Chat Completions. Native Gemini, Anthropic Messages
  or a bespoke protocol are all fine — it is your client.
* Equally, nothing *governs* it. Any developer can point at any endpoint the
  sandbox can reach, and §2.4 measured that the sandbox has **unrestricted
  outbound internet**. If the point of the gateway is to guarantee no model
  traffic escapes, hosted agents need egress control or code review to enforce
  what BYOM enforces structurally.

## 6.8 What you give up by routing around Foundry

Applies to **both** paths, since in both cases Foundry is no longer making the
model call:

| Capability | Effect |
|---|---|
| Deployment content filters | Only apply if the gateway's backend is a filtered Azure deployment |
| Portal token/cost accounting | Not attributed to a Foundry deployment; use gateway telemetry |
| Model Router | Bypassed unless the gateway itself routes to a router deployment |
| PTU / provisioned throughput | Bypassed unless the gateway's backend uses it |
| Model spans in tracing | Agent-level telemetry is retained; per-model spans depend on your instrumentation |
| Responsible AI | **Your responsibility.** Microsoft designates BYOM models as non-Microsoft products, used at your own risk [Documented] |

Also note the data-residency point: a gateway that reaches a non-Azure provider
moves prompt content outside the Azure compliance boundary. For a customer
whose driver is data isolation, that deserves an explicit decision. [Documented]

## 6.9 Native alternative — Model Router

Foundry's **Model Router** now spans OpenAI, Anthropic, Meta, DeepSeek and xAI,
selects per request with tool-awareness, and provides automatic failover.
**Gemini, Mistral and Cohere are not in the pool.** [Documented]

If the driver for the gateway is *"one endpoint, several providers, with
failover"* rather than *"our gateway is a mandated control point"*, Model Router
delivers most of it natively — for every provider except Gemini.

## 6.10 Recommendation

| Situation | Recommendation |
|---|---|
| Provider is Anthropic / Meta / Mistral / DeepSeek / xAI | Deploy natively. Skip the gateway. Check the tool matrix first |
| Requirement is Gemini | Gateway is the **only** option. Prompt agent → `ModelGateway` connection → gateway → Gemini's OpenAI-compatible API |
| Gateway is a mandated governance control point | **Prompt agents + BYOM.** Admin-owned and structurally enforced |
| Want one endpoint with failover, Gemini not required | Evaluate **Model Router** first |
| Need native provider protocols or full client control | **Hosted agent**, with egress control to stop it becoming a bypass |

**If you build the gateway, get these right on day one:** SSE streaming,
OpenAI-compatible `tools` / `tool_choice` / `response_format`, and correct
`deploymentInPath`. The first is the one that silently produces an
uninformative `500`.

---

# 7. Telemetry and metrics

> *Requirement: "the platform must support the injection of DocuSign custom
> telemetry and metrics."*

## 7.1 Hosted agents — custom spans and metrics

A hosted agent emitted a customer-defined span **and** customer-defined metrics
carrying DocuSign's own dimensions, and both were queried back out of
Application Insights:

| Signal | Name | Dimensions that survived |
|---|---|---|
| Span (`dependency`) | `docusign.envelope.validate` | `docusign.marker`, `docusign.envelope_id`, `docusign.tenant` |
| Counter (`customMetric`) | `docusign.envelopes.processed` | `docusign.marker` |
| Histogram (`customMetric`) | `docusign.envelope.latency` | `docusign.marker` |

`force_flush` returned success, so telemetry is queryable within seconds rather
than waiting on process shutdown. [Measured]

Two useful details about the sandbox:

* `APPLICATIONINSIGHTS_CONNECTION_STRING` **and** `AZURE_MONITOR_DISTRO_VERSION`
  are already present, so the OpenTelemetry distro is pre-wired by the platform
  and standard OTel APIs are picked up without the agent configuring an
  exporter. `OTEL_SERVICE_NAME` and `FOUNDRY_AGENT365_TRACING_ENABLED` are set
  too. [Measured]
* Hosted agents can additionally export to a **non-Microsoft** backend over
  OTLP/HTTP. This was measured, not assumed, because DocuSign does not use
  Azure Monitor — see §7.1. [Measured]

## 7.2 Exporting to a non-Azure backend

A stdlib-only OTLP/HTTP receiver (`track-d/otlp-sink/`) was deployed to the
same VNet to stand in for Datadog / Splunk / a self-hosted collector. The agent
was redeployed with `OTEL_EXPORTER_OTLP_ENDPOINT` pointing at it and *no* Azure
Monitor configuration, then invoked.

| What | Result |
|---|---|
| Payloads received by the third-party sink | 49 over five invocations |
| Signals received | `traces`, `metrics`, **and** `logs` |
| Custom span | `docusign.envelope.validate` arrived, with `docusign.marker`, `docusign.envelope_id`, `docusign.correlation_id`, `docusign.tenant` |
| Custom metrics | `docusign.envelopes.processed`, `docusign.envelope.latency` arrived, with their dimensions |
| Resource attributes | `service.name=docusign-hosted-agent`, `docusign.tenant=acme-corp` |
| Content type | `application/x-protobuf` (standard OTLP/HTTP) |

Two findings worth raising on the call:

* **The platform's own telemetry followed.** Payloads also arrived tagged
  `service.name=trackd-plat` carrying `azure.monitor.opentelemetry.performance_counters`,
  and log records containing the model-call URL
  (`POST …/openai/v1/responses`). Setting the standard OTLP env var
  redirects the *platform's* instrumentation as well as the customer's, so
  DocuSign gets the full agent trace — including model calls it did not
  instrument — in its own backend. It also means endpoint URLs leave Azure, so
  the sink must be treated as in-scope for the IP-protection review (§11).
  [Measured]
* **Egress is unrestricted**, consistent with §2.4: the sandbox reached the
  sink directly. A public Datadog endpoint would work the same way; nothing
  needs to be allowlisted. [Measured]

Nothing about this is Azure-specific — the sink is ~90 lines of Python with no
dependencies, and the exporter is the stock `opentelemetry-exporter-otlp-proto-http`
package added to the agent's `requirements.txt`.

## 7.3 Prompt agents

A prompt agent was invoked with `metadata`, `x-client-*` headers, a
`traceparent` and a `baggage` header, and the resulting traces were read back
out of Application Insights.

**The good news — correlation is solved.** Foundry's server spans carried
`operation_Id = 11112222333344445555666677778888`, which is **byte-identical to
the trace id the caller sent**. [Measured]

```text
dependency  invoke_agent ctx-openapi-probe:3    operation_Id=1111...8888
dependency  execute_tool remote_openapi...      operation_Id=1111...8888
dependency  chat gpt-4o-mini-2024-07-18         operation_Id=1111...8888
```

So DocuSign's own telemetry and Microsoft's join on the standard W3C key with no
custom plumbing, whichever backend each lands in. Combined with §5.4.1 — where
the same trace id reaches the internal API — a single trace id spans web tier →
Foundry → tool → internal API.

**The bad news — you cannot add your own dimensions.** The complete customer-
controllable dimension set on `invoke_agent` is: nothing. Every dimension is
Microsoft's:

```text
gen_ai.agent.id / .name / .version      gen_ai.conversation.id
gen_ai.operation.name                   gen_ai.request.model / .response.model
gen_ai.input.messages                   gen_ai.output.messages
gen_ai.tool.definitions                 gen_ai.provider.name
microsoft.foundry.project.id            microsoft.foundry.content_filter.results
microsoft.a365.agent.blueprint.id       span_type
```

Neither `metadata` nor `baggage` appeared as a dimension anywhere. A targeted
query for the customer's own values (`contoso`, `corr-prompt-77`) across
dependencies, requests, traces, customEvents and customMetrics matched **only**
inside `gen_ai.input.messages`, `gen_ai.output.messages` and
`gen_ai.tool.call.arguments` — i.e. as free text, and only because the
model-mediated variant put it there. That is prose in a blob, not a queryable
dimension, and it depends on the model's compliance. **No custom metrics can be
emitted from inside a prompt-agent run.** [Measured]

> ⚠️ **IP-protection note.** `gen_ai.input.messages` and `gen_ai.output.messages`
> contain the **full prompt and completion text**, including system
> instructions. In this configuration DocuSign's prompts — which they consider
> IP (§11) — are written to Application Insights. Confirm content-capture
> settings before enabling tracing in production. [Measured]

**Reaching a non-Azure backend.** The project's tracing target is an
Application Insights connection, so prompt-agent telemetry lands in Azure Monitor
first. Getting it to Datadog or Splunk means an export hop — diagnostic settings
to Event Hub, then a forwarder — rather than the direct OTLP exporter a hosted
agent can use (§7.1). [Documented]

> If "inject DocuSign telemetry and metrics" means *from inside the agent*,
> that is a hosted-agent capability. Prompt agents give you Microsoft's
> telemetry, correctly correlated to your trace id, plus whatever your own
> services record at the tool boundary.

---

# 8. Library integration — DSPy and arbitrary packages

> *Requirement: "the ability to integrate prompt optimisation libraries such
> as DSPy."*

`dspy-ai` was added to `requirements.txt`, deployed, and then *executed* — the
test deliberately runs a real DSPy program rather than only importing it:

| Check | Result |
|---|---|
| Version resolved | **DSPy 3.3.0** |
| Program | `dspy.Predict("question -> answer")` |
| Answer | `Paris` |
| Latency | 3 966 ms |

DSPy drives the model itself through LiteLLM, so this also proves an arbitrary
library can **own the model call** inside the sandbox — not just sit alongside
it. That is the pattern prompt optimisation needs. [Measured]

The sandbox is Python **3.13.14** on Debian, 1 vCPU, and `pip` resolves from
the public index at build time. DSPy is not mentioned anywhere in Foundry's
documentation — it works because the sandbox is a normal Python environment,
not because it is a supported integration. [Measured]

**Prompt agents cannot do this at all.** There is no dependency manifest,
because there is no customer process. The escape hatch is to run the library in
a session pool or an Azure Function and expose it as a tool — which means DSPy
would optimise prompts *outside* the agent and hand results in.

---

# 9. Multi-language support

The agent runtime and the code sandbox are different questions, and the answers
differ.

**The hosted agent runtime is Python-only in practice.** Probing the sandbox
for other runtimes returned nothing:

| Runtime | Present |
|---|---|
| `python3` | **3.13.14** |
| `gcc` | 12.2.0 |
| `bash` | 5.2.15 |
| `node`, `npm`, `dotnet`, `java`, `go` | **absent** |

Subprocess execution *is* allowed. Microsoft documents Python and C# protocol
libraries, plus BYO container images if another runtime is required.
[Measured / Documented]

**Code execution is where multi-language actually lives.** Asking the resource
provider for an invalid pool type makes it list the valid ones:

```
Valid types are: [PythonLTS, CustomContainer, NodeLTS, GpuBase, CsharpLTS, Shell]
```

`CsharpLTS` and `GpuBase` are **not in the public documentation**. [Measured]

Two were taken further than creation:

| Pool type | Result |
|---|---|
| `NodeLTS` | Created and **executed JavaScript** — Node v20.17.0, 5 ms |
| `Shell` | Created and ran shell commands — Ubuntu 22.04.5 with `python3`, `node` v20.17.0, **`java`** and `gcc` present |
| `CsharpLTS` | Type is valid but **not enabled in `eastus2`** (`SessionTypeNotSupportedInRegion`) |

The `Shell` pool is the most interesting result for a multi-language
requirement: one sandbox with Python, Node, Java and a C compiler, driven by
arbitrary shell commands.

> **Shell pools use a different request shape.** `codeInputType` / `code` is
> rejected with `SessionPropertiesMissing`; use `shellCommand` on
> `api-version=2025-02-02-preview`. Older API versions report
> *"Shell execution is not supported in API version 2023-08-01-preview"* even
> when you asked for a newer one. [Measured]

Foundry's built-in Code Interpreter tool remains **Python-only**. [Documented]

---

# 10. Memory, filesystem and state

> *Requirement: "short/long-term memory via filesystem access."*

## 10.1 Filesystem, and what "memory" really means

The hosted sandbox has a writable disk:

| Path | Writable | Note |
|---|---|---|
| `/home/session` | Yes | **3.9 GB free — a separate volume** |
| `/tmp`, `/mnt/data`, `/var/agent-state` | Yes | 2.9 GB free, same volume |
| `/app` | **No** | read-only (`Errno 30`) — your code cannot rewrite itself |

The important question is not "is there a disk" but "does the next turn land on
the same one". Running the same probe three times answers it:

| Turn | Conversation | Sandbox instance | Saw earlier writes | Latency |
|---|---|---|---|---|
| 1 | new | `ddf9dde2` | — | 31.0 s |
| 2 | **chained** (`previous_response_id`) | **`ddf9dde2`** | **yes** | **14.0 s** |
| 3 | new | `1ada359b` | **no** | 31.2 s |

So the filesystem is **conversation-scoped**. [Measured]

* **Short-term memory via filesystem: yes.** Within a conversation the disk
  persists, and the warm turn is also less than half the latency.
* **Long-term memory via filesystem: no.** A new conversation gets a new
  sandbox and an empty disk. Anything that must outlive a conversation belongs
  in a store — Cosmos DB, Azure Storage or Foundry Memory (§14.2).

Microsoft documents session storage as surviving the 15-minute idle timeout and
being deleted after **30 days of inactivity**, with up to 20 GiB at 1 vCPU or
larger. Treat it as a durable cache keyed by conversation, not a system of
record. There is **no documented Azure Files or persistent-volume mount**.
[Documented / Not documented]

## 10.2 Foundry Memory

"No long-term memory" above refers to the **filesystem**, not to the platform.
Foundry Memory is the managed long-term memory service and the architectural
answer to the requirement. The result is mixed.

**What worked.** A memory store was created successfully **from inside the
locked-down VNet**, in 1.08 s:

```text
kind=default  chat_model=gpt-4o-mini  embedding_model=text-embedding-3-small
options: user_profile_enabled, chat_summary_enabled
```

This is worth stating plainly because the documentation warns that memory
stores lack VNet integration: **store creation was not blocked by the private
network.** [Measured]

**What did not work.** Ingesting a conversation with `begin_update_memories`
failed, and kept failing:

```text
ResourceError: Provided Azure resource encountered an error.
  deployment: <project-guid>/deployments/gpt-4o-mini
  details: {"type":"Authentication","status_code":401,
            "description":"Authentication to the Azure OpenAI resource failed."}
```

The Memory service could not authenticate to the model deployment it had been
configured with. Three likely causes were each ruled out:

| Hypothesis | Test | Result |
|---|---|---|
| The VNet is blocking it | Ran the identical probe against a **fully public** project | **Same 401** — not a network problem |
| The project identity lacks rights | Granted `Cognitive Services OpenAI User`, then `Cognitive Services User`, then `Cognitive Services OpenAI Contributor` | **Same 401** after propagation |
| `disableLocalAuth` breaks key auth | Private account has it `true`, public account does not | Both fail **identically** |

So the ingestion path did not work in this subscription, in either network
posture, with or without extra RBAC. Store creation and retrieval APIs respond
normally; it is specifically the model-backed ingestion step that fails.
[Measured]

**How to read this.** Foundry Memory is **preview**, and this is one
subscription in one region — it is not proof the feature is broken for
everyone. But it does mean:

* **Cross-conversation recall and per-user scope isolation are `[Unknown]`
  here** — they were never reached, and this document does not claim measured
  results it does not have.
* Long-term memory should not be scheduled as "already solved" on the strength
  of the documentation. Either validate it early in DocuSign's own tenant, or
  plan the fallback.
* The fallback is well-trodden and already proven in this POC: **conversation
  state in the customer's own Cosmos DB**, measured at 745 ms write / 938 ms
  read over a private endpoint (§10.4). That also keeps memory inside DocuSign's
  subscription, which suits the IP-protection requirement in §11 better than a
  managed store does.

Two SDK details worth noting: the model fields are
`chat_model` / `embedding_model` on `MemoryStoreDefaultDefinition` (not
`*_deployment_name` on the options object), and memory items need an explicit
`"type": "message"` or ingestion fails with *"Failed to parse item with
unknown/missing type"*. [Measured]

## 10.3 Can a hosted agent use Foundry Memory?

Short answer: **it can reach the service, but it does not get the declarative
binding.** The two halves are worth separating.

**The binding is a tool, and hosted agents have no tool list.** Inspecting the
shipped SDK (2.4.0) rather than the docs:

| Model | Relevant fields |
|---|---|
| `MemorySearchPreviewTool` | `memory_store_name`, `scope`, `search_options`, `update_delay` |
| `PromptAgentDefinition` | `instructions`, `model`, **`tools`**, `tool_choice`, `text`, … |
| `HostedAgentDefinition` | `code_configuration`, `container_configuration`, `cpu`, `memory`, `environment_variables`, `protocol_versions`, `telemetry_config` |

Foundry Memory attaches to an agent as the `memory_search_preview` **tool**,
which goes in a `tools` list. `PromptAgentDefinition` has one;
`HostedAgentDefinition` **does not** — a hosted agent brings its own harness, so
there is nothing for the platform to inject a tool into. Retrieval *and* the
automatic write-back implied by `update_delay` are therefore prompt-agent
conveniences. [Measured]

> ⚠️ **Do not misread `HostedAgentDefinition.memory`.** It is the container's
> **RAM** (`1Gi`), not the memory service. The name collision is an easy and
> expensive mistake to make. [Measured]

**But the API is reachable from inside the sandbox.** A `probe_memory` tool was
added to the hosted agent and called the memory API as an ordinary client using
the sandbox's own identity:

```text
list_ok=true  list_ms=204..391  stores=['docusign-longterm-memory']
```

So the hosted agent can see the project's memory stores over the private
network, with no extra RBAC beyond the `Foundry User` role its instance
identity already held. Nothing about hosted agents is fenced off from the
service. [Measured]

**What could not be confirmed.** `search_memories` from inside the sandbox
returned `HttpResponseError: (Timeout) The operation was timeout`, reproduced on
separate invocations while `list()` kept succeeding in the same call. The store
is empty because ingestion is still blocked by the 401 above, so retrieval
quality, cross-conversation recall and scope isolation remain **`[Unknown]`**
for both agent types — this document does not claim them.

One operational detail matters more than the failure itself: the agent turn took
**163 s**, because the search **hung** rather than failing fast. A dependency
that blocks for minutes inside a request path is worse than one that errors
immediately. If DocuSign puts Memory on a user-facing turn, wrap the call in an
explicit client-side timeout and a fallback — do not rely on the service to
fail quickly. [Measured]

**Practical reading for DocuSign.**

* A prompt agent gets managed memory by adding a tool: no code, but no control
  either.
* A hosted agent must **own the memory loop** — search at turn start, write back
  at turn end, from its own code. That is more work, and it is also the option
  that lets DocuSign choose the store, which is what the IP requirement (§11)
  actually wants.
* Given the ingestion failure, the Cosmos-backed pattern already measured in
  §10.4 remains the recommendation for hosted agents until Memory leaves preview.

## 10.4 Agent state, over the private endpoint

The hosted agent wrote and read its own state in a Cosmos account with
`publicNetworkAccess=Disabled`, `disableLocalAuth=true`, using **its own managed
identity** (`Cosmos DB Built-in Data Contributor`), with no keys anywhere:

```text
{'persisted': true, 'auth': 'managed_identity_aad',
 'endpoint_host': '<cosmos-account>.documents.azure.com:443', 'elapsed_ms': 744.96}
{'count': 1, 'items': [{'note': 'sum of squares computed', ...}],
 'auth': 'managed_identity_aad', 'elapsed_ms': 938.15}
```

This is the pattern to recommend: **explicit persistence you control**, rather
than relying on the undocumented mapping between hosted agents and the
Foundry-managed BYO thread store.

---

---

# 11. IP protection — where DocuSign data sits

> *Requirement: "protecting DocuSign IP, including data, memory and session
> state."*

Mostly a consolidation of §2 plus Microsoft's published commitments.

| Asset | Where it lives | Label |
|---|---|---|
| Prompts & completions | Not used to train foundation models; not shared with model providers | [Documented] |
| Threads, messages, files, vector stores | **Customer's own** Cosmos DB, Storage and AI Search under Standard Agent Setup | [Documented] |
| Agent state (this POC) | Customer Cosmos DB, reached over a private endpoint | [Measured, §10.4] |
| Network path | No public egress required; internal APIs reached privately | [Measured, §2] |
| Encryption | AES-256 at rest, optional CMK — **preview features may not support CMK** | [Documented] |
| Hosted agent **source code** | Uploaded as a ZIP; **physical store, retention and CMK support are not documented** | [Not documented] |

Two items worth raising on the call rather than burying:

* **Abuse monitoring may retain content for human review.** Eligible customers
  can apply for **modified abuse monitoring** through the Limited Access
  process, which removes human review and its storage. For a company handling
  signature documents this is usually the first thing legal asks about.
  [Documented]
* **Tracing writes prompts and completions to Application Insights.** Measured
  on a prompt agent: `gen_ai.input.messages` carried the full system
  instructions and user input, `gen_ai.output.messages` the model's reply. If
  prompts are IP, the observability store is part of the IP boundary — apply
  the same retention, RBAC and CMK review you would apply to the agent's data
  store. [Measured]
* **Hosted-agent code confidentiality is not documented.** Microsoft documents
  *that* a ZIP is uploaded, but not where it is stored, how long it is kept
  after deletion, whether CMK applies, or which personnel can access it. If
  agent code is itself DocuSign IP, the **BYO container image** path is the
  safer answer, because the image stays in DocuSign's own registry and Foundry
  pulls it. [Not documented / Documented]

---

# 12. Capability matrix

| Capability | Prompt agent | Hosted agent | Label |
|---|---|---|---|
| **Deployment artifact** | Configuration | Code bundle or container image | [Measured] |
| **Deploy from outside VNet** | n/a (config) | **Blocked, 403** | [Measured] |
| **CI/CD** | Standard | Must run **inside the VNet** | [Measured] |
| **Framework choice** | Foundry-defined | Any — LangGraph verified here | [Measured] |
| **Protocols** | Responses | Responses, Invocations, A2A | [Documented] |
| **Agent identity** | Managed by service | Two Entra identities, **zero roles by default** | [Measured] |
| **Credential injection** | Automatic via data proxy | **You resolve it**, needs RBAC | [Measured] |
| **Private API access** | Yes | Yes | [Measured] |
| **Outbound internet** | Controllable at pool level | **Open unless subnet-restricted** | [Measured] |
| **Cold start** | None measurable | ~15 s/session + slow first turn | [Measured] |
| **Keep-warm control** | Not needed | **None exposed**; do it in the app | [Measured] |
| **Code execution** | Code Interpreter / ACA pool | In-process, or call an ACA pool | [Measured] |
| **Conversation state** | Foundry-managed thread store | **You own it** | [Measured] |
| **Non-OpenAI Azure models** | Yes, incl. tool calling (Grok measured) | Yes | [Measured] |
| **Google Gemini natively** | **Not in the catalog** | Not in the catalog | [Measured] |
| **Customer LLM gateway** | Via `ModelGateway` connection, `<conn>/<model>` | Set `base_url` directly | [Measured] |
| **Gateway must support SSE** | **Yes** — Foundry always streams | No — your client chooses | [Measured] |
| **Gateway use is governable** | Yes, admin owns the connection | **No** — any endpoint the sandbox reaches | [Measured] |
| **State to private Cosmos** | BYO Cosmos supported | Yes — keyless AAD, 745 ms write / 938 ms read | [Measured] |
| **Foundry Tools** | Native `tools=[...]` | Via **Toolbox** MCP endpoint; some tools direct-only | [Documented] |
| **Foundry Memory** — store creation | **Works, incl. inside the VNet** | Works | [Measured] |
| **Foundry Memory** — ingestion | **401 to the model deployment**, public and private alike | Same | [Measured] |
| **Foundry Memory** — recall / scope isolation | Never reached | Never reached | [Unknown] |
| **BYO Cosmos wiring for hosted threads** | Documented containers | **Mapping undocumented** | [Unknown] |
| **Observability** | Portal + tracing | Same, plus your own logs; container log stream is essential | [Measured] |
| **Custom request headers to agent** | No documented mechanism | **`x-client-*` only**; others dropped | [Measured] |
| **Custom context to a downstream API** | **W3C `baggage`** (small entries only) | `x-client-*`, `metadata`, `baggage` | [Measured] |
| **Caller trace id reaches the internal API** | **Yes** | **Yes** | [Measured] |
| **Request `metadata` to agent code** | Conversation metadata (≤16 pairs) | **Yes**, verbatim | [Measured] |
| **W3C trace context into the agent** | Not guaranteed | **Yes** — same trace id end to end | [Measured] |
| **Caller `Authorization` visible to agent** | n/a | **No — always stripped** | [Measured] |
| **Per-user delegated auth (OBO)** | Toolbox/MCP `oauth2`, `user-entra-token` | Same, via Toolbox | [Documented] |
| **Custom OTel spans from inside the agent** | **No** — instrument the caller | **Yes**, with customer dimensions | [Measured] |
| **Custom OTel metrics from inside the agent** | **No** | **Yes** — counter and histogram measured | [Measured] |
| **Export telemetry to non-Azure backend** | Indirect — via Azure Monitor export hop | **Yes** via `OTEL_EXPORTER_OTLP_*`; traces, metrics and logs all arrived | [Measured] |
| **Foundry Memory as a declarative binding** | **Yes** — `memory_search_preview` tool | **No** — no `tools` list on the definition | [Measured] |
| **Foundry Memory API reachable from agent code** | n/a — platform calls it | **Yes** — `list()` in ~200 ms from the sandbox | [Measured] |
| **Custom dimensions on the platform's spans** | **No** — dimension set is entirely Microsoft's | n/a — you own the span | [Measured] |
| **Caller trace id becomes `operation_Id`** | **Yes** | **Yes** | [Measured] |
| **Prompt/completion text written to App Insights** | **Yes** — `gen_ai.input/output.messages` | Yes, if Azure Monitor is configured | [Measured] |
| **Arbitrary PyPI libraries (DSPy)** | **No** | **Yes** — DSPy 3.3.0 ran a real program | [Measured] |
| **Agent runtime language** | n/a | **Python-only in practice**; Python/C# documented, or BYO image | [Measured] |
| **Session pool languages** | `PythonLTS`, `NodeLTS`, `Shell`, `CsharpLTS`, `GpuBase`, `CustomContainer` | Same | [Measured] |
| **Built-in Code Interpreter language** | **Python only** | n/a | [Documented] |
| **Writable filesystem in agent runtime** | n/a | **Yes** — 4.1 GB; `/app` read-only | [Measured] |
| **Filesystem persists across chained turns** | n/a | **Yes** — same sandbox | [Measured] |
| **Filesystem persists across conversations** | n/a | **No** — new sandbox, empty disk | [Measured] |
| **Persistent volume / Azure Files mount** | n/a | **Not documented** | [Unknown] |
| **Where hosted agent source code is stored** | n/a | **Not documented** — use BYO image if code is IP | [Unknown] |
| **Generic OBO to an arbitrary internal API** | No | No — needs a broker | [Documented] |

---

# 13. Operational gotchas

None of these is obvious from the documentation.

| # | Gotcha | Symptom | Fix |
|---|---|---|---|
| 1 | `entry_point` is a **command vector**, not a script path | Deploys fine, `status=active`, then sessions never start; generic *"verify /readiness returns 200"* | `["python", "main.py"]`, not `["main.py"]` |
| 2 | The real failure is only in the container log stream | `bash: line 1: main.py: command not found` | `get_session_log_stream(agent, version, session_id)` |
| 3 | `agent_session_id` on a Responses endpoint | Requests hang until client timeout | Use `previous_response_id` |
| 4 | Session id field is `agent_session_id`, there is no `id` | `DELETE` → `405 Method Not Allowed` | Read `agent_session_id` |
| 5 | Unpinned requests mint sessions endlessly | 160 accumulated, 30-day expiry | Pin conversations; clean up explicitly |
| 6 | Hosted agent identity has no roles | `ClientAuthenticationError` → HTTP 401 | Grant `Foundry User` on the project |
| 7 | Model deployment capacity 1 (=1K TPM) | Turns taking 60–240 s, looks like agent overhead | Check `sku.capacity` before trusting latency |
| 8 | ACR Tasks cannot build against a private ACR | `client with IP ... is not allowed access`, despite `networkRuleBypassOptions: AzureServices` | VNet-connected agent pool |
| 9 | ACA `replicaTimeout` max is 1800 | Executions fail instantly, no logs at all | Keep ≤ 1800 |
| 10 | Python stdout buffering in containers | Output lost when a replica is killed | `PYTHONUNBUFFERED=1` |
| 11 | SDK logs a full traceback per empty 204 | Floods `--tail`, pushes real errors out | Silence the `azure` logger |
| 12 | `az` active subscription is global mutable state | `ResourceGroupNotFound` for resources that exist | Pin `--subscription` on every call |

Further gotchas, all **[Measured]**:

| # | Gotcha | Symptom |
|---|---|---|
| 13 | Foundry's BYOM path **always** sends `"stream": true` | A valid non-streaming gateway reply → 3 silent retries → opaque `500 server_error` |
| 14 | Agent creation never validates `model` | Bogus model or URL returns `200`; fails only at run with `invalid_engine_error` |
| 15 | Connection `metadata` is string→string | A nested JSON array → *"unable to deserialize request body"*; must be `json.dumps`'d |
| 16 | Container Apps has no IMDS | `169.254.169.254` refused instantly; use `IDENTITY_ENDPOINT` + `X-IDENTITY-HEADER` |
| 17 | `/app` is read-only in the hosted sandbox | Writing next to your code fails with `Errno 30`; use `/home/session` |
| 18 | Filesystem state silently disappears between conversations | Chained turns share a sandbox, so local testing looks persistent; a new conversation gets an empty disk |
| 19 | `Shell` session pools reject `code` / `codeInputType` | Use `shellCommand`; older API versions report *"not supported in API version 2023-08-01-preview"* even when you asked for a newer one |
| 20 | `CsharpLTS` and `GpuBase` pool types exist but are undocumented | `CsharpLTS` is not enabled in `eastus2`; discover availability by asking the RP, not the docs |
| 21 | Foundry Memory ingestion returned `401` to its own model deployment | Reproduced on a **public** project too, so it is not the VNet; survived three RBAC grants |
| 22 | Memory items need an explicit `"type": "message"` | Plain `role`/`content` fails with *"Failed to parse item with unknown/missing type"* |
| 23 | Memory model fields are `chat_model` / `embedding_model` | Not `*_deployment_name` on the options object, which raises `TypeError` |
| 24 | `HostedAgentDefinition.memory` is container **RAM**, not Foundry Memory | Reads like a memory-service binding; it takes `1Gi`. The memory service attaches as the `memory_search_preview` *tool*, which hosted agents cannot take |
| 25 | Oversized W3C `baggage` entries are **silently dropped** | ~2 KB arrives intact; at ~8 KB the large entry vanishes with no error while small ones survive — alert on absence, never assume delivery |
| 26 | A new agent version does **not** evict warm sandboxes | The first invoke after publishing `:3` ran `:2`'s code; re-invoke until the reported instance id changes, or you will measure the old build |

SDK shapes that differ from the obvious guess **[Measured]**:

```text
protocol_versions   -> list[ProtocolVersionRecord], not a dict
VersionRefIndicator(agent_version=...)   # not version=
HostedAgentDefinition: container_configuration XOR code_configuration
CodeConfiguration(runtime, entry_point, dependency_resolution)
dependency_resolution in {bundled, remote_build}
connections.get(name, include_credentials=True)
```

---

# 14. Choosing between them

## 14.1 The requirements pull in opposite directions

Custom telemetry from inside the agent, DSPy and filesystem working memory are
**hosted-agent capabilities and not prompt-agent capabilities**. The one
qualification is correlation: prompt-agent spans do carry the caller's trace id,
so DocuSign's telemetry joins Microsoft's even though DocuSign cannot add fields
to it (§7.3). Enforcing the LLM gateway as a governed control point runs the
other way (§6) — a hosted agent can point `base_url` anywhere.

* Telemetry, DSPy and filesystem memory → **hosted agents**.
* Enforcing the LLM gateway as a control point → **prompt agents**.

Resolving that tension is the design decision.

## 14.2 Guidance

**Prefer prompt agents when** the requirement is a governed, network-isolated
agent calling internal APIs and running code in a provable sandbox. Everything
in §2 and §4 is achievable with less to build, no cold start, no agent
identity to provision, and credential handling delegated to the platform.

**Prefer hosted agents when** you need control the prompt-agent model does not
offer:

- a specific framework or an existing LangGraph/LangChain application
- multi-step orchestration, custom checkpointing or state you own outright
- libraries or system dependencies that must live in your own image
- portability of the agent implementation away from a single service

**Accept, if you choose hosted agents:**

1. In-VNet CI/CD — deployment is a data-plane operation.
2. Explicit RBAC for the agent identity, per agent.
3. A real per-session cold start, mitigated in your application rather than by a
   platform setting.
4. Subnet-level egress control, because the account's private setting does not
   provide it.
5. Your own state persistence, rather than the managed thread store.

**A mixed estate is reasonable.** Both models coexist in one project and share
connections, private endpoints and Cosmos. Prompt agents cover governed
mainstream cases; hosted agents cover the workloads that genuinely need a custom
harness.

---

---

# 15. Reproducing

Everything runs through one script, because the data plane is unreachable from
outside the VNet:

```bash
export AZ_SUBSCRIPTION=<subscription-id>

# deploy a hosted LangGraph agent from inside the VNet
./track-d/run-in-vnet.sh deploy_agent.py \
    TRACKD_SRC_DIR=agent-src-api TRACKD_AGENT_NAME=<agent>

# inspect versions and, importantly, the agent identities
./track-d/run-in-vnet.sh inspect_agent.py TRACKD_AGENT_NAME=<agent>

# invoke and assert on tool evidence
./track-d/run-in-vnet.sh invoke_agent.py TRACKD_AGENT_NAME=<agent> \
    TRACKD_PROMPT="What is the status of envelope env-1001?"

# cold start and keep-alive
./track-d/run-in-vnet.sh session_start.py     TRACKD_AGENT_NAME=<agent>
./track-d/run-in-vnet.sh session_keepalive.py TRACKD_AGENT_NAME=<agent> TRACKD_TURNS=6

# diagnose a session that will not start (the only way to see inside)
./track-d/run-in-vnet.sh session_diag.py TRACKD_AGENT_NAME=<agent>

# clean up sessions (dry run by default)
./track-d/run-in-vnet.sh cleanup_sessions.py TRACKD_CLEANUP_APPLY=1

# requirement 4 - what request context survives the ingress
./track-d/run-in-vnet.sh invoke_agent.py TRACKD_AGENT_NAME=<agent> \
    TRACKD_PROMPT="Call echo_request_context and report the raw JSON." \
    'TRACKD_HEADERS=x-client-tenant-id=contoso,x-client-correlation-id=corr-1,traceparent=00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
    'TRACKD_METADATA={"tenant":"contoso"}'
```

Requirement 4 — what a **prompt agent** propagates to an internal API:

```bash
# an echo API that records every header it receives, and serves its own spec
./track-b/context-echo/deploy-echo.sh        # needs ECHO_ACA_ENV=<aca-env-id>

# prompt agent + OpenAPI tool; asserts on the wire, not the model's summary
./track-d/run-in-vnet.sh ctx_openapi_probe.py \
    CTX_PROJECT_ENDPOINT=<project> CTX_ECHO_URL=https://<echo-fqdn>

# the model-mediated variant, and the baggage size ceiling
./track-d/run-in-vnet.sh ctx_openapi_probe.py ... CTX_MODEL_MEDIATED=1
./track-d/run-in-vnet.sh ctx_openapi_probe.py ... CTX_BAGGAGE_PAD=8000
```

Requirement 5 — the LLM gateway:

```bash
# stand up an OpenAI-compatible multi-provider gateway inside the VNet
./track-d/gateway/deploy-gateway.sh

# prompt agent on a native non-OpenAI model, and the negative cases
./track-d/run-in-vnet.sh gateway_probe.py GW_PROJECT_ENDPOINT=<project>

# prompt agent through the ModelGateway connection (BYOM, v2 API)
./track-d/run-in-vnet.sh gateway_agent_probe.py \
    GW_CONNECTION=poc-llm-gateway GW_BASE=<gateway>/v1

# hosted agent calling the same gateway from its own client
./track-d/run-in-vnet.sh deploy_agent.py TRACKD_SRC_DIR=agent-src-gw \
    TRACKD_AGENT_NAME=trackd-gw AGENTENV_GATEWAY_BASE_URL=<gateway>/v1
```

Requirement 6 — telemetry, libraries, filesystem and languages:

```bash
# deploy the probe agent; the connection string is what telemetry flows to
./track-d/run-in-vnet.sh deploy_agent.py TRACKD_SRC_DIR=agent-src-plat \
    TRACKD_AGENT_NAME=trackd-plat \
    AGENTENV_APPLICATIONINSIGHTS_CONNECTION_STRING='<connection-string>' \
    AGENTENV_DOCUSIGN_TENANT=acme-corp

# custom spans and metrics, then confirm they arrived
./track-d/run-in-vnet.sh invoke_agent.py TRACKD_AGENT_NAME=trackd-plat \
    TRACKD_PROMPT='Call probe_telemetry with marker "dsmark-alpha-01".'
az monitor app-insights query --app <app-id> --analytics-query \
    "union dependencies, customMetrics | where name startswith 'docusign'"

# prompt agents: can YOUR context become a dimension on Microsoft's spans?
# (it cannot - this returns matches only inside message-content blobs)
az monitor app-insights query --app <app-id> --analytics-query \
    "union dependencies,requests,traces,customEvents,customMetrics
     | where tostring(customDimensions) has_cs 'contoso'
     | summarize by itemType, name"

# DSPy, and the sandbox runtime inventory
./track-d/run-in-vnet.sh invoke_agent.py TRACKD_AGENT_NAME=trackd-plat \
    TRACKD_PROMPT='Call probe_dspy with question "What is the capital of France?"'
./track-d/run-in-vnet.sh invoke_agent.py TRACKD_AGENT_NAME=trackd-plat \
    TRACKD_PROMPT='Call probe_runtimes and return its raw JSON.'

# is the filesystem memory? three turns, two conversations
./track-d/run-in-vnet.sh fs_memory_probe.py TRACKD_AGENT_NAME=trackd-plat

# telemetry to a NON-Azure backend: stand up a third-party OTLP sink,
# point the agent at it, then read what the sink actually received
./track-d/otlp-sink/deploy-sink.sh          # needs SINK_ACA_ENV=<aca-env-id>
./track-d/run-in-vnet.sh deploy_agent.py TRACKD_SRC_DIR=agent-src-plat \
    TRACKD_AGENT_NAME=trackd-plat \
    AGENTENV_OTEL_EXPORTER_OTLP_ENDPOINT=https://<sink-fqdn>
./track-d/run-in-vnet.sh invoke_agent.py TRACKD_AGENT_NAME=trackd-plat \
    TRACKD_PROMPT='Call probe_telemetry with marker otlp-run-1.'
./track-d/run-in-vnet.sh sink_check.py SINK_BASE_URL=https://<sink-fqdn> \
    'SINK_PATH=/_received?signal=traces&n=4'

# which pool languages exist - ask the RP, not the docs
az rest --method PUT --url ".../sessionPools/probe?api-version=2025-02-02-preview" \
    --body '{"location":"eastus2","properties":{"containerType":"Bogus"}}'
```

| Agent | Source | Demonstrates |
|---|---|---|
| `agent-src/` | LangGraph chat + calculator | Deployability, invocation, cold start |
| `agent-src-api/` | Envelope status tool | Requirement 1 — private API + credential resolution |
| `agent-src-exec/` | `run_python`, context probe, Cosmos state | Requirements 3 and state |
| `agent-src-ctx/` | Echoes `client_headers`, `metadata`, platform ids, trace | Requirement 4 — context propagation |
| `agent-src-gw/` | Calls a customer LLM gateway via `base_url` | Requirement 5 — multi-provider gateway |
| `gateway/` | OpenAI-compatible multi-provider gateway (SSE, audit log) | Requirement 5 — the gateway under test |
| `agent-src-plat/` | Telemetry, DSPy, filesystem and runtime probes | Requirement 6 — platform capabilities |
| `otlp-sink/` | Dependency-free OTLP/HTTP receiver standing in for Datadog | Requirement 6 — telemetry to a non-Azure backend |

> Job environment variables are **sticky** between runs — `run-in-vnet.sh`
> patches the job definition, so a value set once persists until overwritten.
> Pass every variable you care about on every run.
