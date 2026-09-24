"""
Trace + cost-rollup test. No API key needed.

Observability is usually the last thing built and the first thing that's
untested, because "you need the whole system running to see a trace." That is
only true of the LLM spans. The tool and SQL layer runs without a model, so
the trace TREE -- session > delegate > sql, with lineage and cost on every
node -- can be exercised offline, which is what this does.

It scripts the Demo 1 fan-out by calling the real tools under hand-opened
`session` and `delegate` spans (the shape the coordinator would produce),
attaches synthetic token costs to the delegate spans (there are no real
tokens without a model), then reads the emitted trace back off disk and
asserts:

  * a single session root with a delegate child per domain;
  * every SQL query produced an `sql` span carrying its exact text and row
    count -- the lineage claim, checked end to end;
  * per-delegate cost rolls up to the session total.

Run:  make smoke-trace
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from opentelemetry import trace as _otel  # noqa: E402

from proof import trace as ptrace  # noqa: E402
from proof.tools import demand, inventory, production, quality  # noqa: E402

RUN = "smoke_trace"
results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


def scripted_fanout() -> None:
    """The Demo 1 shape, driven by tools directly instead of a model.

    Each `delegate` span stands in for one sub-agent; the tool calls inside it
    emit `sql` spans automatically via proof.tools._base.query.
    """
    with ptrace.span("session", "session", principal="a.morin",
                     model="offline") as root:
        total_in = total_out = 0

        with ptrace.span("delegate:production", "delegate",
                         domain="production") as sp:
            down = production.get_open_downtime()
            ev = down["rows"][0]
            production.estimate_output_loss(ev["line_id"],
                                            ev["operator_eta_minutes"])
            # No model ran, so tokens are synthetic -- the point is that cost
            # lands on the span and rolls up, not the specific number.
            total_in += 1800; total_out += 400
            ptrace.record_usage(sp, 1800, 400)

        with ptrace.span("delegate:inventory", "delegate",
                         domain="inventory") as sp:
            inventory.project_wip_expiry(ev["line_id"],
                                         ev["operator_eta_minutes"])
            total_in += 1500; total_out += 350
            ptrace.record_usage(sp, 1500, 350)

        with ptrace.span("delegate:demand", "delegate", domain="demand") as sp:
            demand.get_orders_for_run(ev["run_id"])
            total_in += 1200; total_out += 300
            ptrace.record_usage(sp, 1200, 300)

        with ptrace.span("delegate:quality", "delegate", domain="quality") as sp:
            quality.get_sku_allergens(category="flatbread")
            total_in += 1100; total_out += 250
            ptrace.record_usage(sp, 1100, 250)

        ptrace.record_usage(root, total_in, total_out)


def main() -> int:
    trace_file = ptrace.TRACE_DIR / f"{RUN}.jsonl"
    if trace_file.exists():
        trace_file.unlink()  # start clean so we assert on this run only

    ptrace.configure(run_label=RUN)
    scripted_fanout()
    _otel.get_tracer_provider().force_flush()

    spans = [json.loads(line) for line in
             trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {s["span_id"]: s for s in spans}
    kind = lambda s: s["attributes"].get("proof.kind")

    sessions = [s for s in spans if kind(s) == "session"]
    delegates = [s for s in spans if kind(s) == "delegate"]
    sqls = [s for s in spans if kind(s) == "sql"]

    print(f"emitted {len(spans)} spans: 1 session, {len(delegates)} delegates, "
          f"{len(sqls)} sql")

    check(len(sessions) == 1, "exactly one session root",
          f"{len(sessions)} found")
    root = sessions[0] if sessions else None

    # Every delegate's parent is the session root -> a proper tree, not a flat
    # list. A flat trace is the usual sign the context isn't propagating.
    if root:
        parented = [d for d in delegates if d["parent_id"] == root["span_id"]]
        check(len(parented) == len(delegates) == 4,
              "four delegate spans, all children of the session",
              f"{len(parented)}/{len(delegates)}")

    # Every SQL span nests under a delegate, carries its query text, and its
    # row count -- the lineage claim, verified through the trace rather than
    # asserted in prose.
    sql_under_delegate = 0
    for s in sqls:
        parent = by_id.get(s["parent_id"])
        if parent and kind(parent) == "delegate":
            sql_under_delegate += 1
    check(sql_under_delegate == len(sqls) and len(sqls) >= 4,
          "every sql span nests under a delegate", f"{sql_under_delegate} of {len(sqls)}")
    check(all(s["attributes"].get("proof.sql") for s in sqls),
          "every sql span carries its query text")
    check(all("proof.row_count" in s["attributes"] for s in sqls),
          "every sql span carries a row count")

    # Cost rollup: the session's recorded cost equals the sum of the delegate
    # costs (within rounding). This is the number that starts a runaway-cost
    # investigation, so it has to add up.
    if root:
        delegate_cost = sum(d["attributes"].get("proof.cost_usd", 0)
                            for d in delegates)
        session_cost = root["attributes"].get("proof.cost_usd", 0)
        agree = abs(delegate_cost - session_cost) < 1e-6
        check(agree, "session cost equals the sum of delegate costs",
              f"session ${session_cost:.4f} vs sum ${delegate_cost:.4f}")
        print(f"cost rollup: {len(delegates)} delegates -> ${session_cost:.4f}")

    # Langfuse readiness: report whether the OTLP exporter would engage,
    # without requiring it. This is the one line that tells you the backend is
    # wired even when you're running offline.
    import os
    lf = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
    print(f"langfuse exporter: {'configured' if lf else 'not configured '
          '(set LANGFUSE_PUBLIC_KEY/SECRET_KEY to enable; file trace still written)'}")

    print(f"\n{'=' * 68}\nresults\n{'=' * 68}")
    for ok, name, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed  "
          f"(trace at data/traces/{RUN}.jsonl)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
