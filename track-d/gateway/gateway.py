"""Requirement 5 - a minimal OpenAI-compatible multi-provider LLM gateway.

Stands in for the customer's real gateway (LiteLLM / APIM / in-house). It:

  * exposes the OpenAI wire surface at /v1/chat/completions
  * routes by requested model name to different upstream providers
  * records every request so we can prove model traffic actually transited it

Routing table:
    gpt-*        -> real Azure OpenAI deployment (keyless, managed identity)
    gemini-*     -> a local stub standing in for Google Gemini, which is NOT in
                    the Azure model catalog. Its only job is to prove a
                    non-Foundry provider can be reached through the gateway
                    from inside a Foundry hosted agent.

Deliberately stdlib-only so it runs on a stock python image with no build step
(the private ACR cannot be built against from outside the VNet).
"""
import json
import os
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AOAI_ENDPOINT = os.environ.get("AOAI_ENDPOINT", "").rstrip("/")
AOAI_API_VERSION = os.environ.get("AOAI_API_VERSION", "2024-10-21")
CLIENT_ID = os.environ.get("GW_CLIENT_ID", "")
PORT = int(os.environ.get("PORT", "8080"))

REQUESTS = []           # in-memory audit log; the evidence for "traffic transited"
LAST = {}               # full request/response bodies of the most recent call
_LOCK = threading.Lock()
_token = {"value": None, "exp": 0}


def aoai_token():
    """Managed-identity token for Azure OpenAI, cached until near expiry.

    Container Apps does not expose the IMDS endpoint that VMs use. It injects
    IDENTITY_ENDPOINT / IDENTITY_HEADER instead, and a request to 169.254.169.254
    is refused outright. IMDS is kept only as a fallback for other hosts.
    """
    now = time.time()
    if _token["value"] and now < _token["exp"] - 120:
        return _token["value"]

    resource = "https://cognitiveservices.azure.com/"
    identity_endpoint = os.environ.get("IDENTITY_ENDPOINT")
    identity_header = os.environ.get("IDENTITY_HEADER")
    if identity_endpoint and identity_header:
        url = f"{identity_endpoint}?api-version=2019-08-01&resource={resource}"
        if CLIENT_ID:
            url += f"&client_id={CLIENT_ID}"
        req = urllib.request.Request(url, headers={"X-IDENTITY-HEADER": identity_header})
    else:
        url = ("http://169.254.169.254/metadata/identity/oauth2/token"
               f"?api-version=2018-02-01&resource={resource}")
        if CLIENT_ID:
            url += f"&client_id={CLIENT_ID}"
        req = urllib.request.Request(url, headers={"Metadata": "true"})

    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    _token["value"] = body["access_token"]
    _token["exp"] = now + int(body.get("expires_in", 3600))
    return _token["value"]


def open_azure_openai(payload, model):
    """Open a connection to the real AOAI deployment named by `model`.

    Returns the raw response object so the caller can either read it whole or
    relay the SSE stream chunk by chunk.
    """
    url = (f"{AOAI_ENDPOINT}/openai/deployments/{model}/chat/completions"
           f"?api-version={AOAI_API_VERSION}")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {aoai_token()}")
    req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, timeout=180)


def to_gemini_stub(payload, model):
    """Stand-in for Google Gemini.

    Gemini is not in the Azure AI Foundry model catalog, so there is no keyless
    Azure path to it. A real deployment would call Vertex AI / Google AI Studio
    here and translate the response into OpenAI shape. The stub returns that
    same OpenAI-shaped envelope so the agent code path is identical.
    """
    user = ""
    for msg in payload.get("messages", []):
        if msg.get("role") == "user":
            user = msg.get("content") or ""

    # Agent Service requires a gateway model to honour the OpenAI tool-call
    # contract. Emit a real tool_call when the caller advertises tools, so the
    # test exercises the same path a tool-using prompt agent would.
    tools = payload.get("tools") or []
    already_called = any(m.get("role") == "tool" for m in payload.get("messages", []))
    if tools and not already_called:
        fn = tools[0].get("function", {})
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_gemini_stub_1",
                "type": "function",
                "function": {
                    "name": fn.get("name", "unknown"),
                    "arguments": json.dumps({"envelope_id": "ENV-1234"}),
                },
            }],
        }
        finish = "tool_calls"
    else:
        message = {
            "role": "assistant",
            "content": (
                f"[served by gateway route=gemini upstream=google-stub "
                f"model={model}] I received: {user[:160]}"
            ),
        }
        finish = "stop"

    return 200, {
        "id": "chatcmpl-gemini-stub",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def gemini_stub_sse(payload, model):
    """Same stub answer, serialised as OpenAI SSE chunks.

    Foundry's BYOM path always sends "stream": true, so a gateway that only
    speaks non-streaming JSON is rejected. Every chunk carries the standard
    chat.completion.chunk envelope.
    """
    _, whole = to_gemini_stub(payload, model)
    msg = whole["choices"][0]["message"]
    finish = whole["choices"][0]["finish_reason"]
    base = {"id": whole["id"], "object": "chat.completion.chunk",
            "created": whole["created"], "model": model}

    def chunk(delta, fin=None):
        body = dict(base)
        body["choices"] = [{"index": 0, "delta": delta, "finish_reason": fin}]
        return body

    yield chunk({"role": "assistant"})
    if msg.get("tool_calls"):
        tc = msg["tool_calls"][0]
        yield chunk({"tool_calls": [{
            "index": 0, "id": tc["id"], "type": "function",
            "function": {"name": tc["function"]["name"], "arguments": ""},
        }]})
        yield chunk({"tool_calls": [{
            "index": 0,
            "function": {"arguments": tc["function"]["arguments"]},
        }]})
    elif msg.get("content"):
        # Split into a few deltas so the client sees a genuine token stream.
        text = msg["content"]
        step = max(1, len(text) // 4)
        for i in range(0, len(text), step):
            yield chunk({"content": text[i:i + step]})
    yield chunk({}, finish)

    if (payload.get("stream_options") or {}).get("include_usage"):
        tail = dict(base)
        tail["choices"] = []
        tail["usage"] = whole["usage"]
        yield tail


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("gw %s" % (fmt % args), flush=True)

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/_audit"):
            with _LOCK:
                self._send(200, {"count": len(REQUESTS), "requests": REQUESTS[-50:]})
        elif self.path.startswith("/_diag"):
            import socket
            out = {"aoai_endpoint": AOAI_ENDPOINT, "hosts": {}}
            # Resolve/connect checks against the configured upstream, so a
            # "502" can be attributed to DNS, routing or identity rather than
            # guessed at.
            host = AOAI_ENDPOINT.split("://")[-1].split("/")[0]
            hosts = [h for h in [host, os.environ.get("GW_DIAG_EXTRA_HOST")] if h]
            for h in hosts:
                rec = {}
                try:
                    rec["dns"] = socket.gethostbyname_ex(h)[2]
                except Exception as exc:  # noqa: BLE001
                    rec["dns"] = f"{type(exc).__name__}: {exc}"
                try:
                    sock = socket.create_connection((h, 443), timeout=8)
                    sock.close()
                    rec["tcp443"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    rec["tcp443"] = f"{type(exc).__name__}: {exc}"
                out["hosts"][h] = rec
            try:
                out["token"] = "ok" if aoai_token() else "empty"
            except Exception as exc:  # noqa: BLE001
                out["token"] = f"{type(exc).__name__}: {exc}"
            self._send(200, out)
        elif self.path.startswith("/_last"):
            with _LOCK:
                self._send(200, LAST or {})
        elif self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": o} for m, o in [
                    ("gpt-4o-mini", "azure-openai"), ("gemini-2.5-pro", "google")]
            ]})
        else:
            self._send(200, {"status": "ok", "service": "poc-llm-gateway"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            self._send(400, {"error": {"message": "invalid json"}})
            return

        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": f"no route for {self.path}"}})
            return

        model = payload.get("model", "")
        route = "gemini" if model.startswith("gemini") else "azure-openai"
        # Record the caller-supplied context headers too - this is what a real
        # gateway would key chargeback and policy off.
        entry = {
            "ts": time.time(),
            "model": model,
            "route": route,
            "path": self.path,
            "ua": self.headers.get("User-Agent", ""),
            # Presence only - never log the credential itself.
            "auth": {
                "api-key": bool(self.headers.get("api-key")),
                "authorization": bool(self.headers.get("Authorization")),
            },
            "client_headers": {k.lower(): v for k, v in self.headers.items()
                               if k.lower().startswith("x-")},
            "has_tools": bool(payload.get("tools")),
        }
        t0 = time.time()
        streaming = bool(payload.get("stream"))
        entry["stream"] = streaming
        try:
            if streaming:
                code, body = self._relay_stream(payload, model, route)
            elif route == "gemini":
                code, body = to_gemini_stub(payload, model)
            else:
                with open_azure_openai(payload, model) as resp:
                    code, body = resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            code, body = exc.code, {"error": {"message": exc.read().decode()[:500]}}
        except Exception as exc:  # noqa: BLE001
            code, body = 502, {"error": {"message": f"{type(exc).__name__}: {exc}"}}
        entry["status"] = code
        entry["latency_ms"] = round((time.time() - t0) * 1000, 1)
        with _LOCK:
            REQUESTS.append(entry)
            LAST.clear()
            LAST.update({
                "request_headers": dict(self.headers.items()),
                "request_body": payload,
                "response_status": code,
                "response_body": body,
            })
        print(f"gw ROUTE model={model} route={route} stream={streaming} "
              f"status={code} ms={entry['latency_ms']}", flush=True)
        if not streaming:
            self._send(code, body)

    def _relay_stream(self, payload, model, route):
        """Emit an SSE response, either from the stub or relayed from AOAI.

        Headers must be written before the first chunk, so any upstream failure
        has to be detected before that point; once bytes are on the wire the
        only honest signal left is closing the stream.
        """
        if route == "gemini":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            sent = 0
            for chunk in gemini_stub_sse(payload, model):
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                sent += 1
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return 200, {"streamed_chunks": sent}

        # Upstream is opened first so a failure still yields a clean JSON error.
        upstream = open_azure_openai(payload, model)
        self.send_response(upstream.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        total = 0
        while True:
            block = upstream.read(4096)
            if not block:
                break
            total += len(block)
            self.wfile.write(block)
            self.wfile.flush()
        upstream.close()
        self.close_connection = True
        return upstream.status, {"relayed_bytes": total}


if __name__ == "__main__":
    print(f"gateway listening on {PORT}; aoai={AOAI_ENDPOINT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
