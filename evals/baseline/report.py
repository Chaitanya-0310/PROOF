"""
Phase 6 report printer — formatted output with assertions.

Follows the project's smoke-test style: a readable report, then [PASS]/[FAIL]
lines with a total, then an honest-limits section.
"""
from __future__ import annotations

import sys
from typing import TextIO

from .metrics import ScenarioBaseline, ScrapAvoidedResult, WipBatchFate


# =====================================================================
# Report formatting
# =====================================================================

def _header(title: str, *, out: TextIO = sys.stdout) -> None:
    out.write(f"\n{'=' * 72}\n{title}\n{'=' * 72}\n")


def _subheader(title: str, *, out: TextIO = sys.stdout) -> None:
    out.write(f"\n  {title}\n  {'-' * len(title)}\n")


def print_scenario_1(b: ScenarioBaseline, *, out: TextIO = sys.stdout) -> None:
    """Print the Demo 1 (line_down) baseline report."""
    s = b.scrap
    t = b.time
    if not s:
        out.write("  (no scrap data for this scenario)\n")
        return

    _header(f"scenario 1: line down ({s.plant_code}/{s.line_code} "
            f"MECHANICAL, ETA {s.restart_eta_minutes}m to restart)", out=out)

    _subheader("the scrap clock", out=out)
    for f in s.batch_detail:
        fate = "survives restart" if f.survives_restart else "EXPIRES"
        out.write(f"    {f.batch_code:12s}  {f.units:>6,} units   "
                  f"expires in {f.minutes_until_expiry:>4}m   "
                  f"salvage deadline: {f.salvage_deadline_minutes:>4.0f}m   "
                  f"{fate}\n")

    _subheader(f"alternate line: {s.alt_line_code} "
               f"(changeover {s.changeover_minutes} min)", out=out)

    agent_label = f"{s.agent_awareness_minutes:.0f}m"
    if t.estimated:
        agent_label += " est"
    manual_label = f"{s.manual_awareness_minutes:.0f}m"

    _subheader(f"scrap avoided (agent at {agent_label}, "
               f"manual at {manual_label})", out=out)

    for f in s.batch_detail:
        if f.survives_restart:
            out.write(f"    {f.batch_code:12s}  survives restart\n")
            continue
        a_str = "YES" if f.agent_can_salvage else "NO "
        m_str = "YES" if f.manual_can_salvage else "NO "
        saved = ""
        if f.saved_by_proof:
            saved = f"  → {f.units:,} units SAVED by PROOF"
        elif f.agent_can_salvage and f.manual_can_salvage:
            saved = "  (both save)"
        elif not f.agent_can_salvage and not f.manual_can_salvage:
            saved = "  (neither saves — needs faster changeover)"
        out.write(f"    {f.batch_code:12s}  PROOF: {a_str} "
                  f"(deadline {f.salvage_deadline_minutes:.0f}m ≥ {agent_label}?)  "
                  f"manual: {m_str} "
                  f"(deadline {f.salvage_deadline_minutes:.0f}m ≥ {manual_label}?)"
                  f"{saved}\n")

    out.write(f"\n    total scrap avoided:         {s.units_saved_by_proof:>6,} units "
              f"({s.batches_saved_by_proof} batch{'es' if s.batches_saved_by_proof != 1 else ''})\n")
    out.write(f"    total WIP expiring:          {s.units_expiring:>6,} units "
              f"({s.batches_expire_before_restart} batches)\n")
    out.write(f"    savings rate:                {s.units_saved_pct:>5.0f}%  "
              f"of expiring WIP\n")

    _subheader("time-to-decision", out=out)
    agent_sec = t.agent_seconds or 0
    out.write(f"    agent:   {agent_sec / 60:>5.1f} min"
              f"{'  (estimated)' if t.estimated else '  (measured from trace)'}\n")
    out.write(f"    manual:  {t.manual_minutes:>5.1f} min  (parameterised, "
              f"industry range 30–45)\n")
    if t.speedup:
        out.write(f"    ratio:   {t.speedup:>5.1f}x faster\n")

    _subheader("total at-risk units (both losses)", out=out)
    out.write(f"    throughput loss (output not produced):  "
              f"{b.throughput_loss_units:>6,} units\n")
    out.write(f"    WIP scrap (dough expires on floor):     "
              f"{s.units_expiring:>6,} units\n")
    out.write(f"    total:                                  "
              f"{b.total_at_risk_units:>6,} units\n")

    if b.at_risk_orders:
        _subheader("strategic orders at risk", out=out)
        for o in b.at_risk_orders:
            out.write(f"    {o.get('order_code', '?'):12s}  "
                      f"{o.get('customer', '?'):30s}  "
                      f"{o.get('units_outstanding', 0):>6,} units outstanding\n")


def print_scenario_2(b: ScenarioBaseline, *, out: TextIO = sys.stdout) -> None:
    """Print the Demo 2 (supplier_slip) baseline report."""
    _header("scenario 2: supplier slip (flour truck late)", out=out)
    t = b.time
    meta = b.alt_line or {}

    late = meta.get("late_pos", [])
    if late:
        _subheader("late purchase orders", out=out)
        for p in late:
            out.write(f"    {p.get('po_code', '?'):12s}  "
                      f"{p.get('supplier', '?'):30s}  "
                      f"{p.get('days_late', '?')} day(s) late  "
                      f"ETA {p.get('eta_at', '?')}\n")

    runout = meta.get("first_runout")
    if runout:
        _subheader("first material runout", out=out)
        out.write(f"    line {runout.get('line_code', '?')}: "
                  f"{runout.get('sku_name', '?')} at "
                  f"{runout.get('planned_start', '?')}\n")

    _subheader("time-to-decision (awareness of supply risk)", out=out)
    agent_sec = t.agent_seconds or 0
    out.write(f"    agent:   {agent_sec / 60:>5.1f} min"
              f"{'  (estimated)' if t.estimated else ''}\n")
    out.write(f"    manual:  {t.manual_minutes:>5.1f} min\n")
    out.write(f"    value: the gap between 'flour runs out at 14:39' known at "
              f"{agent_sec / 60:.0f}m vs {t.manual_minutes:.0f}m is the "
              f"window to expedite the PO\n")


def print_scenario_3(b: ScenarioBaseline, *, out: TextIO = sys.stdout) -> None:
    """Print the Demo 3 (scrap_signal) baseline report."""
    _header("scenario 3: scrap trend deterioration (DAL1/L1)", out=out)
    t = b.time
    meta = b.alt_line or {}

    bp = meta.get("baseline_pct", 0)
    rp = meta.get("recent_pct", 0)
    lift = meta.get("lift_pp", 0)

    _subheader("trend", out=out)
    out.write(f"    baseline scrap:  {bp:.2f}%\n")
    out.write(f"    recent scrap:    {rp:.2f}%\n")
    out.write(f"    deterioration:   {lift:+.2f}pp\n")

    _subheader("time-to-decision (trend detection)", out=out)
    agent_sec = t.agent_seconds or 0
    out.write(f"    agent:   {agent_sec / 60:>5.1f} min (on demand)\n")
    out.write(f"    manual:  until the next monthly review, or never if nobody "
              f"looks\n")
    out.write(f"    value: catching a +{lift:.2f}pp trend immediately vs "
              f"discovering it in a monthly report saves ~30 days of elevated "
              f"scrap\n")


# =====================================================================
# Assertions
# =====================================================================

def run_assertions(
    b1: ScenarioBaseline,
    b2: ScenarioBaseline,
    b3: ScenarioBaseline,
    *,
    out: TextIO = sys.stdout,
) -> int:
    """Run deterministic assertions over the baseline results.

    Returns the number of failures.
    """
    _header("results", out=out)
    passed = 0
    failed = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        tag = "[PASS]" if ok else "[FAIL]"
        suffix = f"  ({detail})" if detail else ""
        out.write(f"  {tag} {label}{suffix}\n")
        if ok:
            passed += 1
        else:
            failed += 1

    # --- Scenario 1: line_down ---
    s = b1.scrap
    t = b1.time

    check("scrap-avoided metric is computable for the planted scenario",
          s is not None)

    if s:
        check("at least one WIP batch is saveable by PROOF but not manually",
              s.batches_saved_by_proof > 0,
              f"got {s.batches_saved_by_proof}")

        check("exactly 2 batches expire before restart",
              s.batches_expire_before_restart == 2,
              f"got {s.batches_expire_before_restart}")

        check("WIP scrap from expiry is 3,450 units",
              s.units_expiring == 3450,
              f"got {s.units_expiring}")

        check("scrap avoided is 1,800 units (1 batch)",
              s.units_saved_by_proof == 1800,
              f"got {s.units_saved_by_proof}")

        check("alternate line exists and changeover is known",
              s.alt_line_code != "N/A" and s.changeover_minutes > 0,
              f"{s.alt_line_code}, {s.changeover_minutes}m")

    check("throughput loss is positive",
          b1.throughput_loss_units > 0,
          f"{b1.throughput_loss_units} units")

    check("total at-risk includes both losses",
          b1.total_at_risk_units > b1.throughput_loss_units
          and b1.total_at_risk_units > (s.units_expiring if s else 0),
          f"{b1.total_at_risk_units} = {b1.throughput_loss_units} + "
          f"{s.units_expiring if s else 0}")

    if t.speedup:
        check("time-to-decision ratio exceeds 10x",
              t.speedup > 10,
              f"{t.speedup:.1f}x")

    # --- Scenario 2: supplier_slip ---
    meta2 = b2.alt_line or {}
    check("late PO is visible",
          len(meta2.get("late_pos", [])) > 0,
          f"{len(meta2.get('late_pos', []))} late PO(s)")
    check("material runout is projectable",
          meta2.get("first_runout") is not None)

    # --- Scenario 3: scrap_signal ---
    meta3 = b3.alt_line or {}
    check("scrap trend deterioration is detectable",
          (meta3.get("lift_pp") or 0) > 0.3,
          f"{meta3.get('lift_pp', 0):+.2f}pp")

    # --- Summary ---
    total = passed + failed
    out.write(f"\n{passed}/{total} passed")
    if failed:
        out.write(f"  ({failed} FAILED)")
    out.write("\n")

    return failed


# =====================================================================
# Honest limits
# =====================================================================

def print_limits(*, out: TextIO = sys.stdout) -> None:
    out.write("""
honest limits of this baseline:
  - manual awareness delay of 35 min is parameterised from the README's
    "30–45 minutes of phone calls", not measured in a real plant.
  - agent response time of 2 min is estimated from demo runs (run
    `make baseline-live` to measure it for real).
  - scrap avoided assumes the single best alternate line is used; whether
    the operator actually moves the batch is a decision, not an outcome.
  - the salvage deadline assumes changeover is the only lead time; in
    reality the batch also needs to physically move and the oven needs
    to be ready.
  - three scenarios, one with a scrap clock. A production baseline needs
    20+ situations across shift patterns and product mixes.
  - no dollar value assigned per unit. The right number depends on the
    product (a $4 artisan loaf vs a $0.25 bun) and whether the scrap
    has a secondary use (animal feed, composting).
""")
