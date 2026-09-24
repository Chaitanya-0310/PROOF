"""Shared plumbing for every domain tool.

Two rules hold across all four domains:

1. **Tools connect as `agent_read`.** They physically cannot write. This is
   enforced by Postgres grants, not by prompt wording, and it is why a
   prompt-injected agent still cannot damage the plant.

2. **Every result carries the SQL that produced it.** Manufacturing people do
   not trust a number without its lineage, and neither should a reviewer.
   The Phase 5 traces and the Demo 3 walkthrough both read `sql` straight off
   the tool result, so this is load-bearing, not decoration.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from proof.db import connect

# One long-lived read-only connection per process. The MCP servers are
# single-purpose subprocesses, so a pool would be ceremony.
_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = connect("agent_read")
    return _conn


def _jsonable(v: Any) -> Any:
    """Make a Postgres value safe for JSON, without losing precision silently."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        # float() here would quietly change the value on money-ish columns.
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def query(sql: str, params: tuple | dict = (), *,
          note: str | None = None) -> dict:
    """Run a read-only query and return rows plus the SQL that produced them.

    The returned shape is what the model sees. It is deliberately small and
    flat: a model given a nested blob will quote the wrong field.

    `params` may be a tuple (for %s placeholders) or a dict (for named
    %(name)s placeholders). The retrieval queries need named parameters
    because they reference the same value several times, and positional
    placeholders there are unreadable and easy to misalign.
    """
    # An `sql` span per query is the bottom of the trace tree, and the one
    # layer that runs WITHOUT a model -- so the observability plumbing is
    # exercised by the no-key smoke tests, not only when an agent drives it.
    # Tracing must never change behaviour or fail a query, so the span is
    # best-effort: if the tracer is misconfigured the query still runs.
    from proof.trace import span  # local import avoids an import cycle

    conn = _get_conn()
    tidied = _tidy(sql)
    with span("sql", "sql", sql=tidied):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            rows = [{c: _jsonable(v) for c, v in zip(cols, r)}
                    for r in cur.fetchall()]
        # A rolled-back read txn keeps the connection from pinning an old
        # snapshot for the life of the process -- these servers are long-lived.
        conn.rollback()
        _otel_set_rows(len(rows))

    # Echo params back in whichever shape they arrived. list(dict) would
    # return only the KEYS, which looks like data and is not.
    if isinstance(params, dict):
        # An embedding literal is ~6KB of digits and would swamp the result
        # the model reads. Summarise it; the query text still shows where it
        # was used.
        shown = {k: (f"<vector[{v.count(',') + 1}]>"
                     if isinstance(v, str) and v.startswith("[") and len(v) > 200
                     else v)
                 for k, v in params.items()}
    else:
        shown = list(params)

    out = {"rows": rows, "row_count": len(rows), "sql": _tidy(sql), "params": shown}
    if note:
        out["note"] = note
    return out


def _otel_set_rows(n: int) -> None:
    """Tag the current span with its row count, if a span is active."""
    from opentelemetry import trace as _otel

    sp = _otel.get_current_span()
    if sp is not None:
        sp.set_attribute("proof.row_count", n)


def _tidy(sql: str) -> str:
    """Collapse the indentation so the SQL reads well inside a tool result."""
    lines = [ln.rstrip() for ln in sql.strip().splitlines()]
    if not lines:
        return ""
    indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
    return "\n".join(ln[indent:] if len(ln) >= indent else ln for ln in lines)


def sim_now_sql() -> str:
    """SQL expression for 'now'.

    Nothing in this project calls now(). Every time comparison goes through
    the simulation clock so demos replay identically -- see README.
    """
    return "(SELECT now_ts FROM ops.sim_clock)"


def as_text(result: dict) -> str:
    """Render a tool result for the model.

    MCP tools return text. JSON is the right format here: it survives the
    model's copy-paste into a final answer far better than an ASCII table,
    and it keeps `sql` adjacent to the rows it produced.
    """
    return json.dumps(result, indent=2, default=str)
