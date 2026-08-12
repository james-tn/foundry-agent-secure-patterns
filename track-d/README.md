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

## Agents

| Source | Demonstrates |
|---|---|
| `agent-src/` | Chat + calculator — deployability, invocation, cold start |
| `agent-src-api/` | Private internal API call and credential resolution |
| `agent-src-exec/` | In-process code execution, context probe, Cosmos state |

## Prerequisites that are easy to miss

- The agent's **instance identity needs an explicit role grant** (`Foundry User`
  on the project) — it starts with none.
- For the state demo, that identity also needs **Cosmos DB Built-in Data
  Contributor**.
- `entry_point` is a command vector: `["python", "main.py"]`.
