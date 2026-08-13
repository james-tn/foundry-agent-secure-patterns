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

## Contents

1. [Executive summary](#1-executive-summary)
2. [Requirement 1 — VNet isolation and internal API access](#2-requirement-1--vnet-isolation-and-internal-api-access)
3. [Requirement 2 — cold start](#3-requirement-2--cold-start)
4. [Requirement 3 — code execution](#4-requirement-3--code-execution)
5. [Requirement 4 — request context propagation and OBO](#5-requirement-4--request-context-propagation-and-obo)
6. [Requirement 5 — using an existing multi-provider LLM gateway](#6-requirement-5--using-an-existing-multi-provider-llm-gateway)
7. [Capability matrix](#7-capability-matrix)
8. [Operational gotchas found the hard way](#8-operational-gotchas-found-the-hard-way)
9. [Choosing between them](#9-choosing-between-them)
10. [Reproducing](#10-reproducing)

---

# 1. Executive summary

All five requirements are satisfiable in a fully private account. Several are
satisfied *differently* enough between the two agent types to change an
architecture review.

| Requirement | Prompt agent | Hosted agent | Verdict |
|---|---|---|---|
| **1. VNet + internal APIs** | Managed runtime calls the API; connection secret injected by the data proxy | Your code calls the API from its own sandbox; you resolve the secret yourself | Both work. Hosted **needs an explicit RBAC grant** that prompt agents never needed |
| **2. Cold start** | No measurable idle de-allocation penalty | **~15 s to provision every new session**, plus a slow first serving turn | **Materially worse.** This is the biggest surprise |
| **3. Code execution** | Delegate to Code Interpreter or an ACA session pool | Run in-process — ~100,000× faster, but **no isolation** | Different trade, not strictly better |
| **4. Request context propagation** | No per-request channel to tools; per-user auth only via Toolbox/MCP connections | **`x-client-*` headers, `metadata` and `traceparent` all measured working** | Hosted is clearly ahead. **Neither offers generic OBO** |
| **5. Existing LLM gateway** | Needs an admin-created **`ModelGateway` connection**; model is `<connection>/<model>` | Just set `base_url` in your own client | Both work. **Gemini is not in the Azure catalog**, so a gateway is the only route to it |

**The four findings most worth raising in a design review:**

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

---

# 2. Requirement 1 — VNet isolation and internal API access

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

## 2.2 Deployment is a data-plane operation — this is the headline

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

# 3. Requirement 2 — cold start

**This is where the two models diverge most, and not in the hosted agent's
favour.**

`FINDINGS.md` §1 establishes that for prompt agents, the "5–10 second
de-allocation latency" concern does not reproduce: the model layer shows a
~1.3 s idle delta, the managed agent runtime shows no measurable penalty and
exposes no warm-up control because it needs none, and the code sandbox costs
~0.36 s.

Hosted agents introduce a layer prompt agents do not have: **your container,
started per session.**

## 3.1 Measured

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

## 3.3 Every unpinned request mints a new session — and pays for it

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
until the client timed out **[Measured]**. This cost real debugging time and is
not obvious from the samples, which show both protocols side by side.

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

# 4. Requirement 3 — code execution

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

## 4.2 The isolation caveat that decides the pattern

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

# 5. Requirement 4 — request context propagation and OBO

> *"Can hosted agents and prompt agents receive, store, and propagate
> customer-defined request context (identity, tenant, correlation IDs) to
> downstream tools and APIs, and what portion of that context can be
> represented through OBO versus custom metadata/header propagation?"*

**Short answer.** Hosted agents have a real, working context channel — measured
end to end. Prompt agents do not expose an equivalent per-request channel to
tools; their user-identity story runs through Toolbox/MCP connection auth
instead. **Neither type gives you generic OBO to an arbitrary internal API**:
the caller's `Authorization` header is deliberately never delivered to agent
code.

## 5.1 What actually arrives — measured

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

> **The trap.** In this POC `x-agent-user-id` came back as the object id of the
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
| Per-request custom headers into tool calls | **No documented mechanism** | [Documented — absence] |
| Conversation/thread metadata | Yes: ≤16 pairs, key ≤64, value ≤512 chars | [Documented] |
| Metadata auto-mapped into OpenAPI tool headers | **No** | [Documented — absence] |
| Direct OpenAPI tool auth options | anonymous, connection (key/token), managed identity — **no user-token option** | [Documented] |
| Per-user auth via MCP/Toolbox connections | **Yes** — `oauth2` and `user-entra-token` | [Documented] |
| W3C trace context into every tool call | Not guaranteed | [Unknown] |

So a prompt agent *can* act with a **specific user's** delegated permissions —
but only through **Toolbox/MCP connections**, where Foundry manages consent,
storage, refresh and injection. It cannot take a correlation id you supplied on
this request and put it in an outbound API header.

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
storage half — private-endpoint Cosmos, keyless, ~745 ms write (§7.1).

**4. `x-client-*` headers for non-authoritative context.**
Correlation ids, locale, feature flags, request tags. Cheap, measured to work.
Validate on arrival; never authorize on it.

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

---

# 6. Requirement 5 — using an existing multi-provider LLM gateway

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

## 6.2 Gemini is not in the catalog — this is the pivotal fact

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

## 6.3 A prompt agent runs happily on a non-OpenAI model

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

To prove it end to end rather than reading about it, this POC deployed a real
OpenAI-compatible gateway (`track-d/gateway/gateway.py`) as a Container App
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

The gateway's own request log is the proof the traffic really transited it —
not model prose. [Measured]

### Four traps that each cost a debugging cycle

**1. BYOM only exists on the v2 prompt-agent API.**
On the legacy `/assistants` surface, `<connection>/<model>` fails with
`invalid_engine_error` exactly like a bogus model name. It works only via
`agents.create_version(PromptAgentDefinition(...))` plus `responses.create()`
with an `agent_reference`. If you are still on `/assistants`, BYOM is not
available to you. [Measured]

**2. Foundry always requests streaming. This is the big one.**
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

**3. Connection metadata must be a JSON *string*.**
`metadata.models` is a map of string→string. Passing a real JSON array is
rejected with *"unable to deserialize request body"*; it has to be
`json.dumps(...)` into a string.

Connection creation also returned `InternalServerError` on three consecutive
attempts before succeeding on the fourth, with an identical payload. **Retry
before believing the payload is wrong.** [Measured]

**4. Container Apps has no IMDS.**
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

Requirement 2 is the one that will cost a day. A gateway that answers with a
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

## 6.9 Native alternative worth considering first

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

# 7. Capability matrix

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
| **Foundry Memory** | Supported | Supported, but **memory stores lack VNet integration** | [Documented] |
| **BYO Cosmos wiring for hosted threads** | Documented containers | **Mapping undocumented** | [Unknown] |
| **Observability** | Portal + tracing | Same, plus your own logs; container log stream is essential | [Measured] |
| **Custom request headers to agent** | No documented mechanism | **`x-client-*` only**; others dropped | [Measured] |
| **Request `metadata` to agent code** | Conversation metadata (≤16 pairs) | **Yes**, verbatim | [Measured] |
| **W3C trace context into the agent** | Not guaranteed | **Yes** — same trace id end to end | [Measured] |
| **Caller `Authorization` visible to agent** | n/a | **No — always stripped** | [Measured] |
| **Per-user delegated auth (OBO)** | Toolbox/MCP `oauth2`, `user-entra-token` | Same, via Toolbox | [Documented] |
| **Generic OBO to an arbitrary internal API** | No | No — needs a broker | [Documented] |

## 7.1 State — measured, over the private endpoint

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

# 8. Operational gotchas found the hard way

Each of these cost real time and none is obvious from the documentation.

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

Requirement 5 added five more **[Measured]**:

| # | Gotcha | Symptom |
|---|---|---|
| 13 | Foundry's BYOM path **always** sends `"stream": true` | A valid non-streaming gateway reply → 3 silent retries → opaque `500 server_error` |
| 14 | Agent creation never validates `model` | Bogus model or URL returns `200`; fails only at run with `invalid_engine_error` |
| 15 | BYOM does not exist on the legacy `/assistants` API | `<conn>/<model>` fails identically to a typo |
| 16 | Connection `metadata` is string→string | A nested JSON array → *"unable to deserialize request body"*; must be `json.dumps`'d |
| 17 | Container Apps has no IMDS | `169.254.169.254` refused instantly; use `IDENTITY_ENDPOINT` + `X-IDENTITY-HEADER` |

Connection creation also returned `InternalServerError` three times in a row
before succeeding on the fourth with an unchanged payload — **retry before
debugging the payload**.

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

# 9. Choosing between them

**Prefer prompt agents when** the requirement is a governed, network-isolated
agent calling internal APIs and running code in a provable sandbox. Everything
in requirement 1 and 3 is achievable with less to build, no cold start, no agent
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

# 10. Reproducing

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

| Agent | Source | Demonstrates |
|---|---|---|
| `agent-src/` | LangGraph chat + calculator | Deployability, invocation, cold start |
| `agent-src-api/` | Envelope status tool | Requirement 1 — private API + credential resolution |
| `agent-src-exec/` | `run_python`, context probe, Cosmos state | Requirements 3 and state |
| `agent-src-ctx/` | Echoes `client_headers`, `metadata`, platform ids, trace | Requirement 4 — context propagation |
| `agent-src-gw/` | Calls a customer LLM gateway via `base_url` | Requirement 5 — multi-provider gateway |
| `gateway/` | OpenAI-compatible multi-provider gateway (SSE, audit log) | Requirement 5 — the gateway under test |

> Job environment variables are **sticky** between runs — `run-in-vnet.sh`
> patches the job definition, so a value set once persists until overwritten.
> Pass every variable you care about on every run.
