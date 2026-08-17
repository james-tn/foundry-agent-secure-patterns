"""Requirement 6 / hosted agent - platform capabilities the customer asked about.

Four separate questions are measured from inside one deployment, because each
one needs the *real* sandbox rather than a local approximation:

  * telemetry   - can the customer emit their own OpenTelemetry spans and
                  metrics, with their own dimensions, to their own App Insights?
  * libraries   - does an arbitrary PyPI package (DSPy) install AND run?
  * filesystem  - what is writable, and what survives between invocations?
                  This is the "short/long-term memory via filesystem" question.
  * runtimes    - which language runtimes exist in the sandbox?

Every tool returns JSON so the evidence is a tool result, not model prose.

`INSTANCE_ID` is minted at import. If two invocations report the same value the
process was reused, which is what makes filesystem state look persistent - an
important distinction for anyone planning to use the disk as memory.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from typing import Annotated

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from langchain_azure_ai.agents.hosting import ResponsesHostServer

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
FOUNDRY_MODEL = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4o-mini")

INSTANCE_ID = str(uuid.uuid4())[:8]
PROCESS_START = time.time()

_credential = DefaultAzureCredential()

# --------------------------------------------------------------------------
# Telemetry: configured once at import, the way a real agent would do it.
# --------------------------------------------------------------------------
_TELEMETRY: dict = {"configured": False}


def _configure_otlp(endpoint: str) -> None:
    """Export to a non-Microsoft backend over OTLP/HTTP.

    Stands in for Datadog / Splunk / a self-hosted collector: the SDK and the
    wire format are identical, only the URL differs.
    """
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "customer-hosted-agent"),
        "customer.tenant": os.environ.get("CUSTOMER_TENANT", "acme-corp"),
        "customer.component": "envelope-agent",
    })
    base = endpoint.rstrip("/")

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces", timeout=15))
    )
    trace.set_tracer_provider(tp)

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics", timeout=15),
        export_interval_millis=15000,
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    _TELEMETRY["configured"] = True
    _TELEMETRY["exporter"] = "otlp-http"
    _TELEMETRY["otlp_endpoint_host"] = base.split("//")[-1].split("/")[0]


def _configure_telemetry() -> None:
    otlp = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    _TELEMETRY["otlp_endpoint_present"] = bool(otlp)
    if otlp:
        _TELEMETRY["source"] = "otlp-env"
        try:
            _configure_otlp(otlp)
        except Exception as exc:  # noqa: BLE001
            _TELEMETRY["error"] = f"otlp: {type(exc).__name__}: {str(exc)[:300]}"
        return

    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    _TELEMETRY["connection_string_present"] = bool(conn)
    _TELEMETRY["source"] = "env" if conn else "none"
    if not conn:
        _TELEMETRY["error"] = "no APPLICATIONINSIGHTS_CONNECTION_STRING in sandbox"
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=conn,
            # A customer dimension applied to every signal this agent emits.
            resource_attributes={
                "service.name": os.environ.get("OTEL_SERVICE_NAME", "customer-hosted-agent"),
                "customer.tenant": os.environ.get("CUSTOMER_TENANT", "acme-corp"),
                "customer.component": "envelope-agent",
            },
        )
        _TELEMETRY["configured"] = True
        _TELEMETRY["exporter"] = "azure-monitor-opentelemetry"
    except Exception as exc:  # noqa: BLE001
        _TELEMETRY["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"


_configure_telemetry()


@tool
def probe_telemetry(
    marker: Annotated[str, "Unique marker so the span can be found in App Insights."],
) -> str:
    """Emit a custom OpenTelemetry span and custom metrics, then report the result."""
    out = dict(_TELEMETRY)
    out["marker"] = marker
    out["instance_id"] = INSTANCE_ID
    try:
        from opentelemetry import metrics, trace

        tracer = trace.get_tracer("customer.agent.custom")
        with tracer.start_as_current_span("customer.envelope.validate") as span:
            # Customer-defined dimensions on a customer-defined span.
            span.set_attribute("customer.marker", marker)
            span.set_attribute("customer.envelope_id", "env-1001")
            span.set_attribute("customer.tenant", os.environ.get("CUSTOMER_TENANT", "acme-corp"))
            span.set_attribute("customer.correlation_id", str(uuid.uuid4()))
            ctx = span.get_span_context()
            out["trace_id"] = format(ctx.trace_id, "032x")
            out["span_id"] = format(ctx.span_id, "016x")
            out["span_recording"] = span.is_recording()
            time.sleep(0.05)

        meter = metrics.get_meter("customer.agent.custom")
        counter = meter.create_counter(
            "customer.envelopes.processed", unit="1", description="Envelopes processed"
        )
        counter.add(3, {"customer.marker": marker, "customer.result": "ok"})
        hist = meter.create_histogram(
            "customer.envelope.latency", unit="ms", description="Envelope latency"
        )
        hist.record(42.5, {"customer.marker": marker})
        out["metrics_emitted"] = ["customer.envelopes.processed", "customer.envelope.latency"]

        # Force export so the signal is queryable without waiting on shutdown.
        try:
            trace.get_tracer_provider().force_flush(10000)
            metrics.get_meter_provider().force_flush(10000)
            out["flushed"] = True
        except Exception as exc:  # noqa: BLE001
            out["flushed"] = False
            out["flush_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
    return json.dumps(out)


@tool
def probe_dspy(
    question: Annotated[str, "A question to answer through a real DSPy program."],
) -> str:
    """Import DSPy and run a real DSPy program against the Foundry model."""
    out = {"instance_id": INSTANCE_ID}
    try:
        import dspy

        out["dspy_version"] = getattr(dspy, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001
        return json.dumps({**out, "ok": False,
                           "error": f"import failed: {type(exc).__name__}: {str(exc)[:300]}"})

    try:
        project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_credential)
        base_url = str(project.get_openai_client().base_url)
        token = _credential.get_token(_AZURE_AI_SCOPE).token

        # DSPy drives the model itself through LiteLLM, so this also proves an
        # arbitrary library can own the model call inside the sandbox.
        lm = dspy.LM(
            f"openai/{FOUNDRY_MODEL}",
            api_base=base_url,
            api_key=token,
            model_type="chat",
            max_tokens=200,
        )
        dspy.configure(lm=lm)

        classify = dspy.Predict("question -> answer")
        t0 = time.time()
        pred = classify(question=question)
        out["ok"] = True
        out["latency_ms"] = round((time.time() - t0) * 1000, 1)
        out["dspy_answer"] = str(pred.answer)[:400]
        out["program"] = "dspy.Predict('question -> answer')"
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
    return json.dumps(out)


def _probe_path(path: str) -> dict:
    """Can we write here, and is anything from a previous invocation still here?"""
    entry: dict = {"path": path}
    marker_file = os.path.join(path, "memory_probe.jsonl")
    try:
        os.makedirs(path, exist_ok=True)
        prior = []
        if os.path.exists(marker_file):
            with open(marker_file) as fh:
                prior = [json.loads(line) for line in fh if line.strip()]
        entry["existed_before"] = bool(prior)
        entry["prior_writes"] = len(prior)
        entry["prior_instances"] = sorted({p.get("instance_id") for p in prior})[:5]
        with open(marker_file, "a") as fh:
            fh.write(json.dumps({"instance_id": INSTANCE_ID, "ts": time.time()}) + "\n")
        entry["writable"] = True
        usage = shutil.disk_usage(path)
        entry["free_gb"] = round(usage.free / 1e9, 1)
        entry["total_gb"] = round(usage.total / 1e9, 1)
    except Exception as exc:  # noqa: BLE001
        entry["writable"] = False
        entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return entry


@tool
def probe_filesystem() -> str:
    """Test which paths are writable and what survived from earlier invocations."""
    candidates = [
        "/tmp",
        os.getcwd(),
        os.path.expanduser("~"),
        "/mnt/data",
        "/var/agent-state",
        "/app",
    ]
    seen, paths = set(), []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        paths.append(_probe_path(path))
    return json.dumps({
        "instance_id": INSTANCE_ID,
        "process_age_s": round(time.time() - PROCESS_START, 1),
        "cwd": os.getcwd(),
        "paths": paths,
    })


@tool
def probe_runtimes() -> str:
    """Report the sandbox runtime: Python version, OS, and other language runtimes."""
    runtimes = {}
    for name, args in {
        "node": ["node", "--version"],
        "npm": ["npm", "--version"],
        "dotnet": ["dotnet", "--version"],
        "java": ["java", "-version"],
        "go": ["go", "version"],
        "gcc": ["gcc", "--version"],
        "bash": ["bash", "--version"],
    }.items():
        exe = shutil.which(args[0])
        if not exe:
            runtimes[name] = None
            continue
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
            runtimes[name] = ((proc.stdout or proc.stderr).strip().splitlines() or [""])[0][:60]
        except Exception as exc:  # noqa: BLE001
            runtimes[name] = f"error: {type(exc).__name__}"

    interesting = ("FOUNDRY", "AZURE", "APPLICATIONINSIGHTS", "OTEL", "IDENTITY",
                   "MSI", "PORT", "AGENT", "CUSTOMER")
    return json.dumps({
        "instance_id": INSTANCE_ID,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "hostname": platform.node(),
        "runtimes": runtimes,
        "can_subprocess": shutil.which("bash") is not None,
        "env_keys": sorted(k for k in os.environ if any(t in k.upper() for t in interesting)),
    })


@tool
def probe_memory(
    scope: Annotated[str, "Memory scope (namespace) to search, e.g. user-alice."],
    query: Annotated[str, "What to recall from long-term memory."],
) -> str:
    """Call the Foundry Memory service directly from inside the hosted sandbox.

    Hosted agents get no declarative memory binding - the memory_search_preview
    tool attaches to a prompt agent definition, which a hosted agent does not
    have. So the only route is to be an ordinary client of the memory API using
    the sandbox's own identity. This measures whether that route works.
    """
    out = {"scope": scope, "store": os.environ.get("MEMORY_STORE", "poc-longterm-memory")}
    try:
        from azure.ai.projects import AIProjectClient

        client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_credential)
        memory = client.beta.memory_stores

        started = time.time()
        stores = [s.name for s in memory.list()]
        out["list_ok"] = True
        out["list_ms"] = int((time.time() - started) * 1000)
        out["stores"] = stores[:10]

        started = time.time()
        result = memory.search_memories(out["store"], scope=scope, items=query)
        items = list(
            getattr(result, "results", None) or getattr(result, "data", None) or []
        )
        out["search_ok"] = True
        out["search_ms"] = int((time.time() - started) * 1000)
        out["hits"] = len(items)
        out["recalled"] = [str(getattr(i, "content", i))[:160] for i in items[:3]]

        # Ingestion previously failed 401 when called from a job identity; retry
        # here because the sandbox identity is a different principal.
        if os.environ.get("MEMORY_TRY_INGEST") == "1":
            started = time.time()
            try:
                poller = memory.begin_update_memories(
                    out["store"],
                    scope=scope,
                    items=[
                        {"type": "message", "role": "user",
                         "content": "I always sign envelopes with a click-to-sign signature."},
                    ],
                )
                poller.result()
                out["ingest_ok"] = True
            except Exception as exc:  # noqa: BLE001
                out["ingest_ok"] = False
                out["ingest_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            out["ingest_ms"] = int((time.time() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
    return json.dumps(out)


def _build_chat_model() -> ChatOpenAI:
    project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_credential)
    openai_client = project.get_openai_client()
    token_provider = get_bearer_token_provider(_credential, _AZURE_AI_SCOPE)
    return ChatOpenAI(
        model=FOUNDRY_MODEL,
        base_url=str(openai_client.base_url),
        api_key=token_provider,
        use_responses_api=True,
        output_version="responses/v1",
    )


def main() -> None:
    graph = create_agent(
        _build_chat_model(),
        tools=[probe_telemetry, probe_dspy, probe_filesystem, probe_runtimes, probe_memory],
    )
    ResponsesHostServer(graph).run(port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
