"""A stand-in for a non-Azure observability backend (Datadog, Honeycomb, ...).

Many customers do not send telemetry to Azure Monitor, so the question is not
"does Foundry emit telemetry" but "can the agent ship it somewhere Microsoft
does not own". This receiver is the smallest thing that can answer it: it
speaks the OTLP/HTTP surface, accepts `/v1/traces`, `/v1/metrics` and
`/v1/logs`, and records what arrived.

It deliberately does **not** parse protobuf. Pulling printable strings out of
the payload is enough to prove *our* spans and metric names arrived, and it
keeps the receiver dependency-free so it runs on a stock python image with no
build step - the same constraint that shaped the LLM gateway.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "4318"))
_LOCK = threading.Lock()
_RECEIVED: list[dict] = []
_PRINTABLE = re.compile(rb"[ -~]{4,}")


def _strings(body: bytes) -> list[str]:
    return [m.decode(errors="ignore") for m in _PRINTABLE.findall(body)]


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
        if self.path.startswith("/_received"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            want = query.get("signal", [None])[0]
            limit = int(query.get("n", ["8"])[0])
            with _LOCK:
                data = list(_RECEIVED)
            names = sorted({n for entry in data for n in entry["customer_names"]})
            shown = [e for e in data if want is None or e["signal"] == want]
            self._send(200, {
                "count": len(data),
                "signals": sorted({entry["signal"] for entry in data}),
                "customer_names": names,
                "recent": shown[-limit:],
            })
        elif self.path.startswith("/_reset"):
            with _LOCK:
                _RECEIVED.clear()
            self._send(200, {"reset": True})
        else:
            self._send(200, {"ok": True, "endpoints": ["/v1/traces", "/_received"]})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        signal = self.path.rstrip("/").split("/")[-1]
        found = _strings(body)
        entry = {
            "signal": signal,
            "path": self.path,
            "bytes": len(body),
            "content_type": self.headers.get("Content-Type", ""),
            # Proof it is *our* telemetry rather than the platform's.
            "customer_names": sorted({s for s in found if "customer" in s.lower()}),
            "sample": found[:12],
        }
        with _LOCK:
            _RECEIVED.append(entry)
        # OTLP/HTTP expects an empty protobuf ExportServiceResponse; an empty
        # body with 200 is accepted by the SDK exporters.
        self.send_response(200)
        self.send_header("Content-Type", self.headers.get("Content-Type", "application/x-protobuf"))
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
