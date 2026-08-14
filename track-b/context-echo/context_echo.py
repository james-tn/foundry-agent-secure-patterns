"""Track B / Requirement 4 - what context actually reaches a downstream API?

A prompt agent's OpenAPI tool calls this server. Every inbound request is
recorded verbatim so the question "can a prompt agent propagate caller context
to an internal API" is answered by inspecting the wire, not by asking the model.

Deliberately dependency-free and self-describing: it serves its own OpenAPI
spec, so the agent can be pointed at it with no build step.

  GET /internal/envelopes/{envelope_id}   the tool operation
  GET /_last                              what the server actually received
  GET /_reset                             clear the recording
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://localhost:{PORT}")

_LOCK = threading.Lock()
_RECEIVED: list[dict] = []

ENVELOPES = {
    "env-1001": {"envelope_id": "env-1001", "status": "completed", "document_name": "Mutual NDA"},
    "env-1002": {"envelope_id": "env-1002", "status": "sent", "document_name": "MSA"},
}

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Internal Context Echo API", "version": "1.0.0"},
    "servers": [{"url": PUBLIC_URL}],
    "paths": {
        "/internal/envelopes/{envelope_id}": {
            "get": {
                "operationId": "getEnvelopeStatus",
                "summary": "Get an internal envelope's status.",
                "parameters": [
                    {
                        "name": "envelope_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "The envelope identifier, e.g. env-1001.",
                    },
                    # Present so the model-mediated path can be tested: can the
                    # agent be instructed to carry a correlation id itself?
                    {
                        "name": "correlation_id",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": (
                            "Caller correlation id for audit. Pass through the "
                            "correlation id you were given for this request."
                        ),
                    },
                ],
                "responses": {"200": {"description": "Envelope status"}},
            }
        }
    },
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = dict(urllib.parse.parse_qs(parsed.query))

        if path == "/openapi.json":
            self._send(200, SPEC)
            return
        if path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if path == "/_last":
            with _LOCK:
                data = list(_RECEIVED)
            self._send(200, {"count": len(data), "recent": data[-6:]})
            return
        if path == "/_reset":
            with _LOCK:
                _RECEIVED.clear()
            self._send(200, {"reset": True})
            return

        if path.startswith("/internal/envelopes/"):
            envelope_id = path.rsplit("/", 1)[-1]
            # The whole point: record every header, unfiltered.
            entry = {
                "path": self.path,
                "query": query,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
            with _LOCK:
                _RECEIVED.append(entry)
            self._send(200, ENVELOPES.get(envelope_id, {"error": "not found"}))
            return

        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
