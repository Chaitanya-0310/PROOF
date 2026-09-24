"""
Tracing for the agent system.

Instrumentation is vendor-neutral OpenTelemetry. That is a deliberate choice:
the spans in the code describe WHAT happened (a delegation, a tool call, a
query) without naming a backend, so pointing them at Langfuse -- or Phoenix,
or Grafana, or a flat file -- is an exporter swap, not a rewrite.

Two exporters:

  * A local JSONL file, always on. It costs nothing, needs no service, and
    means the no-key smoke tests still produce a real trace to assert on.
    This is what makes observability testable BEFORE the model is involved.

  * Langfuse over OTLP, when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are
    set. Langfuse ingests OpenTelemetry directly, so this is just a second
    span processor pointed at its endpoint. It lights up with real
    generations the moment an ANTHROPIC_API_KEY is also present -- until then
    there are no LLM calls to show, only the deterministic tool/SQL spans.

The span attribute vocabulary is small and prefixed `proof.*` so a trace is
readable without a schema:

    proof.kind         session | coordinator | delegate | tool | sql
    proof.domain       production | inventory | demand | quality | actions
    proof.tool         the tool or delegate name
    proof.sql          the exact query a tool ran (lineage, end to end)
    proof.row_count    rows returned
    proof.input_tokens / proof.output_tokens / proof.cost_usd
    proof.principal    who the session is acting for
"""
from __future__ import annotations

import base64
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry import trace as _otel
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from .agents.config import estimate_cost

TRACE_DIR = Path(__file__).resolve().parent.parent / "data" / "traces"
_LOCK = threading.Lock()
_CONFIGURED = False


class JsonlFileSpanExporter(SpanExporter):
    """Append each finished span to a JSONL file, one compact object per line.

    OTel ships a ConsoleSpanExporter, but it pretty-prints multi-line JSON
    that is a chore to parse back. The smoke test reads these spans and
    asserts on the tree shape and the cost rollup, so one-object-per-line is
    worth the few lines it takes to write.
    """

    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans) -> SpanExportResult:
        try:
            with _LOCK, self._path.open("a", encoding="utf-8") as f:
                for s in spans:
                    ctx = s.get_span_context()
                    parent = s.parent.span_id if s.parent else None
                    f.write(json.dumps({
                        "name": s.name,
                        "trace_id": f"{ctx.trace_id:032x}",
                        "span_id": f"{ctx.span_id:016x}",
                        "parent_id": f"{parent:016x}" if parent else None,
                        "start_ns": s.start_time,
                        "end_ns": s.end_time,
                        "duration_ms": (s.end_time - s.start_time) / 1e6,
                        "attributes": dict(s.attributes or {}),
                        "status": s.status.status_code.name,
                    }, default=str) + "\n")
            return SpanExportResult.SUCCESS
        except Exception:  # noqa: BLE001 - a trace failure must never fail the run
            return SpanExportResult.FAILURE


def _langfuse_processor() -> BatchSpanProcessor | None:
    """An OTLP exporter pointed at Langfuse, or None when unconfigured.

    Langfuse authenticates the OTLP endpoint with HTTP Basic using the
    public/secret key pair. Cloud and self-hosted are the same code -- only
    LANGFUSE_HOST differs -- which is the whole argument for going through
    OTLP rather than a vendor SDK.
    """
    public = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret = os.getenv("LANGFUSE_SECRET_KEY")
    if not (public and secret):
        return None

    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )

    exporter = OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {auth}"},
    )
    return BatchSpanProcessor(exporter)


def configure(run_label: str | None = None) -> None:
    """Install the tracer provider once per process.

    Idempotent: importing and calling this from several entry points (the CLI,
    the eval runner, a smoke test) must not stack duplicate processors.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    resource = Resource.create({
        "service.name": "proof",
        "proof.run_label": run_label or os.getenv("PROOF_RUN_LABEL", "adhoc"),
    })
    provider = TracerProvider(resource=resource)

    # Local file: SimpleSpanProcessor so spans land immediately and in order,
    # which the offline smoke test relies on.
    fname = (run_label or "trace").replace("/", "_")
    provider.add_span_processor(
        SimpleSpanProcessor(JsonlFileSpanExporter(TRACE_DIR / f"{fname}.jsonl")))

    lf = _langfuse_processor()
    if lf is not None:
        provider.add_span_processor(lf)

    _otel.set_tracer_provider(provider)
    _CONFIGURED = True


def tracer():
    if not _CONFIGURED:
        configure()
    return _otel.get_tracer("proof")


@contextmanager
def span(name: str, kind: str, **attrs: Any):
    """Open a span with the proof.* attribute convention.

    Nested spans nest automatically through OTel's context, so a `delegate`
    span opened inside a `coordinator` span becomes its child with no manual
    parent wiring.
    """
    with tracer().start_as_current_span(name) as sp:
        sp.set_attribute("proof.kind", kind)
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(f"proof.{k}", v)
        yield sp


def record_usage(sp, input_tokens: int, output_tokens: int) -> float:
    """Attach token counts and a dollar estimate to a span; return the cost.

    Cost lives on the span, not just in a summary line, so a trace answers
    'what did this one delegation cost?' -- which is the question that starts
    the runaway-cost investigation the Phase 5 write-up is about.
    """
    cost = estimate_cost(input_tokens, output_tokens)
    sp.set_attribute("proof.input_tokens", input_tokens)
    sp.set_attribute("proof.output_tokens", output_tokens)
    sp.set_attribute("proof.cost_usd", round(cost, 6))
    return cost
