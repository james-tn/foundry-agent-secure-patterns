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
5. [Capability matrix](#5-capability-matrix)
6. [Operational gotchas found the hard way](#6-operational-gotchas-found-the-hard-way)
7. [Choosing between them](#7-choosing-between-them)
8. [Reproducing](#8-reproducing)

---

# 1. Executive summary

All three requirements are satisfiable with hosted agents in a fully private
account. Two of them are satisfied *differently* enough to change an
architecture review.

| Requirement | Prompt agent | Hosted agent | Verdict |
|---|---|---|---|
| **1. VNet + internal APIs** | Managed runtime calls the API; connection secret injected by the data proxy | Your code calls the API from its own sandbox; you resolve the secret yourself | Both work. Hosted **needs an explicit RBAC grant** that prompt agents never needed |
| **2. Cold start** | No measurable idle de-allocation penalty | **~15 s to provision every new session**, plus a slow first serving turn | **Materially worse.** This is the biggest surprise |
| **3. Code execution** | Delegate to Code Interpreter or an ACA session pool | Run in-process — ~100,000× faster, but **no isolation** | Different trade, not strictly better |

**The three findings most worth raising in a design review:**

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

# 5. Capability matrix

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
| **State to private Cosmos** | BYO Cosmos supported | Yes — keyless AAD, 745 ms write / 938 ms read | [Measured] |
| **Foundry Tools** | Native `tools=[...]` | Via **Toolbox** MCP endpoint; some tools direct-only | [Documented] |
| **Foundry Memory** | Supported | Supported, but **memory stores lack VNet integration** | [Documented] |
| **BYO Cosmos wiring for hosted threads** | Documented containers | **Mapping undocumented** | [Unknown] |
| **Observability** | Portal + tracing | Same, plus your own logs; container log stream is essential | [Measured] |

## 5.1 State — measured, over the private endpoint

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

# 6. Operational gotchas found the hard way

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

# 7. Choosing between them

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

# 8. Reproducing

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
```

| Agent | Source | Demonstrates |
|---|---|---|
| `agent-src/` | LangGraph chat + calculator | Deployability, invocation, cold start |
| `agent-src-api/` | Envelope status tool | Requirement 1 — private API + credential resolution |
| `agent-src-exec/` | `run_python`, context probe, Cosmos state | Requirements 3 and state |

> Job environment variables are **sticky** between runs — `run-in-vnet.sh`
> patches the job definition, so a value set once persists until overwritten.
> Pass every variable you care about on every run.
