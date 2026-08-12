"""Track D / D0 - hosted-agent deployability spike.

Packages the LangGraph agent in agent-src/ and attempts to create a hosted
agent version through the project data plane.

The point of this probe is to determine *where* the deployment call has to run
from when the Foundry account has publicNetworkAccess=Disabled. Run it from a
workstation and from inside the VNet and compare the classified result.
"""
import hashlib
import io
import os
import pathlib
import sys
import time
import zipfile

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "probes"))
import _config  # noqa: E402

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.ai.projects.models import (  # noqa: E402
    CodeConfiguration,
    HostedAgentDefinition,
    ProtocolVersionRecord,
)
from azure.identity import DefaultAzureCredential  # noqa: E402

SRC = pathlib.Path(__file__).resolve().parent / os.environ.get("TRACKD_SRC_DIR", "agent-src")
AGENT_NAME = os.environ.get("TRACKD_AGENT_NAME", "trackd-langgraph")


def build_zip() -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(SRC.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(SRC).as_posix())
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _agent_env(deployment: str, endpoint: str) -> dict:
    env = {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": deployment,
        "FOUNDRY_PROJECT_ENDPOINT": endpoint,
    }
    # Anything prefixed AGENTENV_ is forwarded to the hosted agent container.
    for key, value in os.environ.items():
        if key.startswith("AGENTENV_"):
            env[key[len("AGENTENV_"):]] = value
    return env


def classify(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    text = str(exc)
    if status == 403 and "Public access is disabled" in text:
        return "BLOCKED_PUBLIC_ACCESS_DISABLED"
    if status == 403:
        return "BLOCKED_403_OTHER"
    if status == 401:
        return "BLOCKED_401_AUTH"
    if status in (404, 400):
        return f"REJECTED_{status}"
    return f"ERROR_{status or type(exc).__name__}"


def main() -> int:
    endpoint = _config.get("OAI_PROJECT_ENDPOINT")
    deployment = _config.get("OAI_DEPLOYMENT", default="gpt-4o", required=False)

    data, digest = build_zip()
    print(f"AGENT_NAME={AGENT_NAME}")
    print(f"SRC_DIR={SRC.name}")
    print(f"ZIP_BYTES={len(data)} SHA256={digest[:16]}...")
    print(f"ENDPOINT_HOST={endpoint.split('/')[2]}")

    definition = HostedAgentDefinition(
        cpu="0.5",
        memory="1Gi",
        code_configuration=CodeConfiguration(
            runtime="python_3_13",
            entry_point=["python", "main.py"],
            dependency_resolution="remote_build",
        ),
        environment_variables=_agent_env(deployment, endpoint),
        protocol_versions=[
            ProtocolVersionRecord(protocol="responses", version="2.0.0")
        ],
    )

    stream = io.BytesIO(data)
    stream.name = "code.zip"

    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    started = time.time()
    try:
        version = client.agents.create_version_from_code(
            AGENT_NAME,
            definition=definition,
            code=stream,
            code_zip_sha256=digest,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.time() - started
        print(f"ELAPSED_S={elapsed:.2f}")
        print(f"RESULT={classify(exc)}")
        print(f"DETAIL={str(exc)[:400]}")
        return 1

    elapsed = time.time() - started
    print(f"ELAPSED_S={elapsed:.2f}")
    print("RESULT=DEPLOY_ACCEPTED")
    print(f"VERSION={getattr(version, 'version', None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
