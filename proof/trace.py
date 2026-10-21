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
        sp.set_attribute("langfuse.observation.type", _LF_TYPE.get(kind, "span"))
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(f"proof.{k}", v)
        yield sp


def record_usage(sp, input_tokens: int, output_tokens: int,
                 model: str | None = None, cost_usd: float | None = None) -> float:
    """Attach token counts and a dollar estimate to a span; return the cost.

    Cost lives on the span, not just in a summary line, so a trace answers
    'what did this one delegation cost?' -- which is the question that starts
    the runaway-cost investigation the Phase 5 write-up is about.

    `cost_usd`, when given, is used as-is: a span covering calls to several
    models (the session, under a router) cannot be priced from its token
    total at a single rate.
    """
    cost = (cost_usd if cost_usd is not None
            else estimate_cost(input_tokens, output_tokens, model))
    sp.set_attribute("proof.input_tokens", input_tokens)
    sp.set_attribute("proof.output_tokens", output_tokens)
    sp.set_attribute("proof.cost_usd", round(cost, 6))
    return cost


# --- Langfuse presentation ---------------------------------------------------
# Langfuse reads these attribute names off OTLP spans (see its OpenTelemetry
# docs). Everything below is plain span attributes, so the JSONL exporter and
# any other OTel backend still see the same data.

# proof.kind -> Langfuse observation type, so the trace tree shows agents,
# tools, retrievals and generations as distinct things rather than "span".
_LF_TYPE = {
    "session": "agent", "coordinator": "agent", "delegate": "agent",
    "tool": "tool", "sql": "retriever", "router": "chain",
    "generation": "generation",
}

# Prompts, tool results and rows can be large. Past this the value is cut and
# says so, because a silently clipped input is worse than a marked one.
MAX_ATTR_CHARS = int(os.getenv("PROOF_TRACE_MAX_CHARS", "200000"))


def _dump(v: Any) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    if len(s) > MAX_ATTR_CHARS:
        s = s[:MAX_ATTR_CHARS] + f"... [truncated, {len(s) - MAX_ATTR_CHARS} more chars]"
    return s


def set_io(sp, *, input: Any = None, output: Any = None) -> None:
    """Attach what went in and what came out, in the fields Langfuse displays."""
    if input is not None:
        sp.set_attribute("langfuse.observation.input", _dump(input))
    if output is not None:
        sp.set_attribute("langfuse.observation.output", _dump(output))


def set_trace_info(sp, *, name: str, user: str | None = None,
                   session_id: str | None = None, tags: list[str] | None = None,
                   input: Any = None, **metadata: Any) -> None:
    """Trace-level fields (name, user, tags, input) from the root span."""
    sp.set_attribute("langfuse.trace.name", name)
    if user:
        sp.set_attribute("user.id", user)
    if session_id:
        sp.set_attribute("session.id", session_id)
    if tags:
        sp.set_attribute("langfuse.trace.tags", tags)
    if input is not None:
        sp.set_attribute("langfuse.trace.input", _dump(input))
    for k, v in metadata.items():
        if v is not None:
            sp.set_attribute(f"langfuse.trace.metadata.{k}", _dump(v))


def set_trace_output(sp, output: Any) -> None:
    sp.set_attribute("langfuse.trace.output", _dump(output))


def emit_span(name: str, kind: str, start_ns: int, end_ns: int, *,
              input: Any = None, output: Any = None, **attrs: Any) -> None:
    """A finished child span with explicit times, under the current span.

    For work measured from outside (the SQL a tool ran: the MCP server is a
    separate process, so the client reports it after the fact).
    """
    sp = tracer().start_span(name, start_time=start_ns)
    sp.set_attribute("proof.kind", kind)
    sp.set_attribute("langfuse.observation.type", _LF_TYPE.get(kind, "span"))
    for k, v in attrs.items():
        if v is not None:
            sp.set_attribute(f"proof.{k}", v)
    set_io(sp, input=input, output=output)
    sp.end(end_time=end_ns)


def start_generation(name: str, model: str, body: dict, agent: str):
    """Open a Langfuse `generation` for one model request.

    Parented on whatever span is current, so the call lands under the agent
    that made it. Not made current itself: it is ended from the response hook.
    """
    sp = tracer().start_span(name)
    sp.set_attribute("proof.kind", "generation")
    sp.set_attribute("proof.agent", agent)
    sp.set_attribute("langfuse.observation.type", "generation")
    sp.set_attribute("langfuse.observation.model.name", model)
    sp.set_attribute("gen_ai.request.model", model)
    params = {k: body[k] for k in ("max_tokens", "temperature", "thinking",
                                   "tool_choice") if k in body}
    if params:
        sp.set_attribute("langfuse.observation.model.parameters", _dump(
            {k: (v if isinstance(v, (int, float, str)) else json.dumps(v))
             for k, v in params.items()}))
    tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
    if tools:
        sp.set_attribute("langfuse.observation.metadata.tools", _dump(tools))
    set_io(sp, input={"system": body.get("system"),
                      "messages": body.get("messages")})
    return sp


def end_generation(sp, status: int, response: dict | None, raw: str = "") -> None:
    """Close a generation with the model's reply, tokens and cost."""
    if response is None or status >= 400:
        sp.set_attribute("langfuse.observation.level", "ERROR")
        sp.set_attribute("langfuse.observation.status_message", f"HTTP {status}")
        set_io(sp, output=raw[:5000] if raw else None)
        sp.end()
        return

    model = response.get("model") or ""
    if model:
        sp.set_attribute("gen_ai.response.model", model)
    # The full content array: thinking, text and tool_use blocks. That is the
    # agent's reasoning and its decision to call a tool, as the model wrote it.
    set_io(sp, output={"role": "assistant", "content": response.get("content"),
                       "stop_reason": response.get("stop_reason")})
    sp.set_attribute("proof.stop_reason", str(response.get("stop_reason")))

    u = response.get("usage") or {}
    tin, tout = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    usage = {"input": tin, "output": tout, "total": tin + tout}
    for k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if u.get(k):
            usage[k] = int(u[k])
    sp.set_attribute("langfuse.observation.usage_details", json.dumps(usage))
    cost = estimate_cost(tin, tout, model or None)
    sp.set_attribute("langfuse.observation.cost_details", json.dumps({"total": round(cost, 6)}))
    sp.end()


def flush(timeout_ms: int = 10000) -> None:
    """Push buffered spans to Langfuse now (batching otherwise delays them)."""
    try:
        prov = _otel.get_tracer_provider()
        if hasattr(prov, "force_flush"):
            prov.force_flush(timeout_ms)
    except Exception:  # noqa: BLE001 - tracing never fails a run
        pass
