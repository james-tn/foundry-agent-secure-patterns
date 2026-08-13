# Track D — hosted agents

Re-runs the three customer requirements using **hosted agents** (your own code,
LangGraph/LangChain harness) instead of prompt agents, in the same locked-down
Foundry account.

Results and analysis: [`../HOSTED-VS-PROMPT-AGENTS.md`](../HOSTED-VS-PROMPT-AGENTS.md)

## Why every script goes through `run-in-vnet.sh`

Hosted-agent deployment and invocation are **data-plane** operations. With
`publicNetworkAccess=Disabled` they return `403` from outside the network, and
ARM does not expose agents at all. So each script is packaged and executed by a
container job inside the delegated subnet.

```bash
export AZ_SUBSCRIPTION=<subscription-id>
./run-in-vnet.sh <script.py> [KEY=VALUE ...]
```

Job environment variables are **sticky** between runs — the script patches the
job definition, so a value set once persists until overwritten. Pass every
variable you care about on every run.

## Scripts

| Script | Purpose |
|---|---|
| `deploy_agent.py` | Zip an `agent-src*` directory and create an agent version |
| `inspect_agent.py` | List versions and, importantly, the two agent identities |
| `invoke_agent.py` | Invoke and print `TOOL_CALL` / `TOOL_OUTPUT` evidence |
| `session_start.py` | Cold start: explicit `create_session`, poll to `ACTIVE` |
| `session_keepalive.py` | Steady-state latency with conversations pinned |
| `session_diag.py` | Stream hosted-container logs — the only way to see inside |
| `cleanup_sessions.py` | Delete accumulated sessions (dry run unless `TRACKD_CLEANUP_APPLY=1`) |
| `discover_invoke.py` | Probe that found the invoke route and api-version |
| `gateway_probe.py` | Requirement 5: native non-OpenAI models, and what `model` will not accept |
| `gateway_agent_probe.py` | Requirement 5: BYOM through a `ModelGateway` connection on the v2 agents API |
| `fs_memory_probe.py` | Requirement 6: is the sandbox filesystem memory? Three turns across two conversations |
| `memory_probe.py` | Requirement 6: Foundry Memory — create a store, ingest, recall across conversations, check scope isolation |
| `sink_check.py` | Requirement 6: read the third-party OTLP sink's received-telemetry log (its FQDN only resolves in-VNet) |

## Agents

| Source | Demonstrates |
|---|---|
| `agent-src/` | Chat + calculator — deployability, invocation, cold start |
| `agent-src-api/` | Private internal API call and credential resolution |
| `agent-src-exec/` | In-process code execution, context probe, Cosmos state |
| `agent-src-ctx/` | Request context, `metadata` and `traceparent` propagation |
| `agent-src-gw/` | Calling a customer LLM gateway directly via `base_url` |
| `agent-src-plat/` | Requirement 6: custom telemetry and metrics, DSPy, filesystem, runtime inventory |
| `otlp-sink/` | Requirement 6: dependency-free OTLP/HTTP receiver standing in for Datadog / Splunk / a self-hosted collector |

## Gateway

`gateway/` is a stdlib-only, OpenAI-compatible **multi-provider LLM gateway**
used as the system under test for Requirement 5. It routes by model name
(`gemini-*` to a non-Azure stub, `gpt-*` to the real Azure OpenAI deployment),
speaks **SSE** — which Foundry's BYOM path requires — and keeps an in-memory
audit log at `/_audit` so "the traffic really transited the gateway" is evidence
rather than an assumption. Deploy with `gateway/deploy-gateway.sh`.

It carries no image build step on purpose: the private ACR cannot be built
against from outside the VNet, so the source is injected as a base64 env var and
decoded at start-up.

## Prerequisites that are easy to miss

- The agent's **instance identity needs an explicit role grant** (`Foundry User`
  on the project) — it starts with none.
- For the state demo, that identity also needs **Cosmos DB Built-in Data
  Contributor**.
- `entry_point` is a command vector: `["python", "main.py"]`.
