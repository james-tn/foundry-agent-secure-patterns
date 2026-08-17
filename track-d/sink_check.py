"""Track D - read the OTLP sink's received-telemetry log from inside the VNet.

The Container Apps environment is VNet-injected, so its FQDN only resolves on
the private DNS zone. This runs in the same VNet as the sink.
"""
import json
import os
import urllib.request

BASE = os.environ["SINK_BASE_URL"].rstrip("/")
PATH = os.environ.get("SINK_PATH", "/_received")

with urllib.request.urlopen(f"{BASE}{PATH}", timeout=60) as resp:
    data = json.loads(resp.read().decode())

print(f"F SINK_STATUS={resp.status}")
print(f"F COUNT={data.get('count')}")
print(f"F SIGNALS={data.get('signals')}")
print(f"F CUSTOMER_NAMES={data.get('customer_names')}")
for entry in data.get("recent", []):
    print(
        f"F RECV signal={entry['signal']} bytes={entry['bytes']} "
        f"ctype={entry['content_type']} names={entry['customer_names']}"
    )
    print(f"F   SAMPLE={entry['sample']}")
print("F RESULT=SINK_READ_OK")
