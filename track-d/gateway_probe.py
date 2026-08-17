"""Requirement 5 - LLM gateway / multi-provider model access.

Runs inside the VNet. Answers, with tool-call evidence rather than model prose:

  G1  Can a PROMPT agent be created against a non-OpenAI model deployment
      (xAI Grok) in the same Foundry project, and can it call a tool?
  G2  Does the prompt agent's `model` field accept an arbitrary
      OpenAI-compatible gateway base URL (i.e. can a prompt agent be pointed
      at the customer's own gateway at all)?
  G3  Which connection types does the project expose that could carry an
      external model provider?

Every result is printed as RESULT[key]=value so the ACA log tail can be
parsed without relying on log-line ordering.
"""
import json
import os
import time
import urllib.request
import urllib.error

from azure.identity import DefaultAzureCredential

API = "2025-11-15-preview"
ENDPOINT = os.environ["GW_PROJECT_ENDPOINT"].rstrip("/")
GROK_DEPLOYMENT = os.environ.get("GW_GROK_DEPLOYMENT", "grok-fast")
OAI_DEPLOYMENT = os.environ.get("GW_OAI_DEPLOYMENT", "gpt-4o-mini")

_cred = DefaultAzureCredential()


def emit(key, value):
    # ACA log lines are split on newlines and truncated, so flatten and chunk.
    flat = " ".join(str(value).split())
    if len(flat) <= 380:
        print(f"RESULT[{key}]={flat}", flush=True)
        return
    for i in range(0, len(flat), 380):
        print(f"RESULT[{key}#{i // 380}]={flat[i:i + 380]}", flush=True)


def call(method, path, body=None, base=None):
    """Raw data-plane call. Returns (status, parsed_or_text)."""
    token = _cred.get_token("https://ai.azure.com/.default").token
    url = f"{base or ENDPOINT}{path}"
    url += ("&" if "?" in url else "?") + f"api-version={API}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001 - want the class name in the log
        return -1, f"{type(exc).__name__}: {exc}"


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_envelope_status",
        "description": "Look up the status of an envelope by id.",
        "parameters": {
            "type": "object",
            "properties": {"envelope_id": {"type": "string"}},
            "required": ["envelope_id"],
        },
    },
}


def run_prompt_agent(model, label):
    """Create a prompt agent on `model`, run one turn, report tool-call evidence."""
    status, agent = call("POST", "/assistants", {
        "name": f"gw-probe-{label}",
        "model": model,
        "instructions": "You look up envelope status. Always use the tool.",
        "tools": [WEATHER_TOOL],
    })
    emit(f"{label}.create_status", status)
    if status >= 300:
        emit(f"{label}.create_error", json.dumps(agent)[:600])
        return
    agent_id = agent["id"]
    emit(f"{label}.agent_id", agent_id)
    emit(f"{label}.model_echo", agent.get("model"))

    try:
        s, thread = call("POST", "/threads", {})
        if s >= 300:
            emit(f"{label}.thread_error", json.dumps(thread)[:400])
            return
        tid = thread["id"]
        call("POST", f"/threads/{tid}/messages", {
            "role": "user", "content": "What is the status of envelope ENV-1234?",
        })
        t0 = time.time()
        s, run = call("POST", f"/threads/{tid}/runs", {"assistant_id": agent_id})
        if s >= 300:
            emit(f"{label}.run_error", json.dumps(run)[:600])
            return
        rid = run["id"]
        # Poll to a terminal state. requires_action means the model emitted a
        # tool call, which is the evidence we actually care about.
        for _ in range(60):
            s, run = call("GET", f"/threads/{tid}/runs/{rid}")
            if run.get("status") in ("completed", "failed", "requires_action",
                                     "cancelled", "expired"):
                break
            time.sleep(2)
        elapsed = round(time.time() - t0, 2)
        emit(f"{label}.run_status", run.get("status"))
        emit(f"{label}.run_seconds", elapsed)
        if run.get("last_error"):
            # Emit fields separately: the ACA log pipeline mangles raw JSON.
            err = run["last_error"]
            emit(f"{label}.err_code", err.get("code"))
            emit(f"{label}.err_msg", err.get("message"))
        ra = run.get("required_action") or {}
        calls = (ra.get("submit_tool_outputs") or {}).get("tool_calls") or []
        emit(f"{label}.tool_calls", len(calls))
        if calls:
            fn = calls[0].get("function", {})
            emit(f"{label}.tool_name", fn.get("name"))
            emit(f"{label}.tool_args", json.dumps(fn.get("arguments"))[:200])
            call("POST", f"/threads/{tid}/runs/{rid}/cancel", {})
    finally:
        call("DELETE", f"/assistants/{agent_id}")
        emit(f"{label}.cleaned_up", "yes")


def gateway_tests():
    """Requirement 5 - can a prompt agent run on a customer gateway model?"""
    conn = os.environ.get("GW_CONNECTION", "poc-llm-gateway")

    # Direct reachability of the gateway from inside the VNet, so a later agent
    # failure can be attributed to Foundry rather than to the network.
    for path, tag in [("/", "root"), ("/v1/models", "models")]:
        try:
            with urllib.request.urlopen(
                    os.environ["GW_BASE"].replace("/v1", "") + path, timeout=30) as r:
                emit(f"g4_reach.{tag}", f"{r.status}:{r.read().decode()[:120]}")
        except Exception as exc:  # noqa: BLE001
            emit(f"g4_reach.{tag}", f"{type(exc).__name__}: {exc}")

    # The documented reference form is "<connection-name>/<model-name>".
    run_prompt_agent(f"{conn}/gemini-2.5-pro", "g5_gw_gemini")
    run_prompt_agent(f"{conn}/gpt-4o-mini", "g6_gw_aoai")

    # Gateway-side proof that the traffic actually transited the gateway.
    try:
        with urllib.request.urlopen(
                os.environ["GW_BASE"].replace("/v1", "") + "/_audit", timeout=30) as r:
            audit = json.loads(r.read().decode())
        emit("g7_audit.count", audit.get("count"))
        for i, rec in enumerate(audit.get("requests", [])[-6:]):
            emit(f"g7_audit.rec{i}",
                 " | ".join(f"{k}={v}" for k, v in rec.items()))
    except Exception as exc:  # noqa: BLE001
        emit("g7_audit.error", f"{type(exc).__name__}: {exc}")


def main():
    emit("endpoint", ENDPOINT)
    if os.environ.get("GW_ONLY") == "1":
        gateway_tests()
        return

    # --- G1: non-OpenAI model (xAI Grok) driving a prompt agent + tool call
    run_prompt_agent(GROK_DEPLOYMENT, "g1_grok")

    # --- baseline: same probe on the OpenAI deployment, so a failure above is
    # attributable to the model family and not to the probe itself.
    run_prompt_agent(OAI_DEPLOYMENT, "g0_oai")

    # --- G2: does `model` accept an arbitrary gateway base URL?
    for candidate, tag in [
        ("https://my-gateway.internal/v1/chat/completions", "g2_url"),
        ("gemini-2.5-pro", "g2_gemini_name"),
    ]:
        s, body = call("POST", "/assistants", {
            "name": "gw-probe-reject", "model": candidate,
            "instructions": "probe",
        })
        emit(f"{tag}.status", s)
        if s < 300:
            emit(f"{tag}.create_ACCEPTED", body.get("model"))
            # Creation does not validate `model`. The only real test is whether
            # a run against it resolves, so drive one turn to a terminal state.
            aid = body["id"]
            _, th = call("POST", "/threads", {})
            call("POST", f"/threads/{th['id']}/messages", {"role": "user", "content": "hi"})
            rs, run = call("POST", f"/threads/{th['id']}/runs", {"assistant_id": aid})
            for _ in range(30):
                if run.get("status") in ("completed","failed","requires_action","expired","cancelled"):
                    break
                time.sleep(2)
                _, run = call("GET", f"/threads/{th['id']}/runs/{run['id']}")
            emit(f"{tag}.run_status", run.get("status"))
            err = run.get("last_error") or {}
            emit(f"{tag}.run_error_code", err.get("code"))
            emit(f"{tag}.run_error_msg", str(err.get("message"))[:250])
            call("DELETE", f"/assistants/{aid}")
        else:
            msg = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
            emit(f"{tag}.error", str(msg)[:300])

    # --- G3: connection types available in the project
    gateway_tests()

    s, conns = call("GET", "/connections")
    emit("g3.connections_status", s)
    if s < 300 and isinstance(conns, dict):
        for c in conns.get("value", []):
            emit("g3.connection", f"{c.get('name')}|{c.get('type')}")


if __name__ == "__main__":
    main()
