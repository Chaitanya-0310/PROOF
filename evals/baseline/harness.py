"""
Phase 6 baseline harness — run the planted scenarios and measure value.

Offline mode (no API key): queries the database, computes scrap-avoided and
time-to-decision from the planted state with a parameterised agent response
time. This is the deterministic, CI-friendly path.

Live mode (--live, needs API key): runs the coordinator on the demo question,
captures the OTel trace, and measures the actual agent response time.

The harness reuses the same domain tools the agents use, so the numbers here
and the numbers the agents report can never disagree.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Repo root so `proof.*` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.db import load_env, connect, sim_now  # noqa: E402
from proof.tools import production, inventory, demand  # noqa: E402
from evals.baseline.metrics import (  # noqa: E402
    ScenarioBaseline,
    compute_scrap_avoided,
    compute_time_to_decision,
)

load_env()

# =====================================================================
# Constants
# =====================================================================

# The README says 30–45 minutes. 35 is the midpoint and a reasonable
# default. Vary it with --manual-delay to see the sensitivity.
DEFAULT_MANUAL_DELAY_MINUTES = 35.0

# A typical PROOF demo finishes in 1.5–3 minutes. 2 is a conservative
# default; live mode replaces it with the measured value.
DEFAULT_AGENT_ESTIMATE_MINUTES = 2.0


# =====================================================================
# Scenario 1: line down — the scrap clock
# =====================================================================

def run_scenario_1_offline(
    agent_minutes: float = DEFAULT_AGENT_ESTIMATE_MINUTES,
    manual_minutes: float = DEFAULT_MANUAL_DELAY_MINUTES,
) -> ScenarioBaseline:
    """Compute the baseline metrics for the Demo 1 line-down scenario.

    Everything comes from the database — no model involved.
    """
    # 1. Find the stopped line
    down = production.get_open_downtime("TOR1")["rows"]
    if not down:
        raise RuntimeError("No open downtime at TOR1 — run `make scenario` first")
    event = down[0]
    line_id = event["line_id"]
    run_id = event["run_id"]
    line_code = event["line_code"]
    eta = event["operator_eta_minutes"] or 90

    # 2. The scrap clock: what expires before restart?
    wip = inventory.project_wip_expiry(line_id, eta)["rows"]

    # 3. Throughput loss
    loss = production.estimate_output_loss(line_id, eta)["rows"]
    throughput_loss = loss[0]["units_not_produced"] if loss else 0

    # 4. The best alternate line
    line_status = production.get_line_status(line_id)["rows"]
    sku_id = None
    if line_status:
        # get_line_status returns sku metadata; we need the sku_id for
        # find_alternate_lines. Extract it from the running run.
        with connect("agent_read") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sku_id FROM ops.production_runs WHERE run_id = %s",
                    (run_id,))
                row = cur.fetchone()
                sku_id = row[0] if row else None

    alt_line_code = "N/A"
    changeover = 0
    alt_detail = None
    if sku_id:
        alts = production.find_alternate_lines(sku_id, "TOR1")["rows"]
        # Pick the first genuinely free line
        free = [a for a in alts
                if a["currently_running_run"] is None
                and not a["is_currently_down"]]
        if free:
            alt_detail = free[0]
            alt_line_code = alt_detail["line_code"]
            changeover = alt_detail["changeover_minutes"]

    # 5. Compute scrap avoided
    scrap = compute_scrap_avoided(
        wip_rows=wip,
        restart_minutes=eta,
        changeover_minutes=changeover,
        alt_line_code=alt_line_code,
        plant_code="TOR1",
        line_code=line_code,
        scenario="line_down",
        agent_awareness_minutes=agent_minutes,
        manual_awareness_minutes=manual_minutes,
    )

    # 6. Orders at risk
    at_risk = demand.get_at_risk_orders("TOR1", 24)["rows"]
    strategic = [o for o in at_risk if o.get("priority_tier") == "strategic"]

    # 7. Time-to-decision (estimated in offline mode)
    ttd = compute_time_to_decision(
        scenario="line_down",
        agent_estimate_seconds=agent_minutes * 60,
        manual_minutes=manual_minutes,
    )

    wip_scrap = scrap.units_expiring
    return ScenarioBaseline(
        scenario="line_down",
        scrap=scrap,
        time=ttd,
        throughput_loss_units=throughput_loss,
        total_at_risk_units=throughput_loss + wip_scrap,
        at_risk_orders=strategic[:4],
        alt_line=alt_detail,
    )


# =====================================================================
# Scenario 2: supplier slip — time-to-awareness for supply risk
# =====================================================================

def run_scenario_2_offline(
    agent_minutes: float = DEFAULT_AGENT_ESTIMATE_MINUTES,
    manual_minutes: float = DEFAULT_MANUAL_DELAY_MINUTES,
) -> ScenarioBaseline:
    """Scenario 2: the flour truck is late.

    No WIP scrap clock here — the value is awareness of a supply gap that
    would otherwise be discovered when the line runs out of flour.
    """
    pos = inventory.get_open_purchase_orders("TOR1", "FLR-HRS")["rows"]
    late = [p for p in pos if (p.get("days_late") or 0) > 0]

    runout = inventory.project_material_runout("TOR1", "FLR-HRS")["rows"]
    first_negative = next(
        (r for r in runout if (r.get("balance_after_run") or 0) < 0), None)

    ttd = compute_time_to_decision(
        scenario="supplier_slip",
        agent_estimate_seconds=agent_minutes * 60,
        manual_minutes=manual_minutes,
    )

    return ScenarioBaseline(
        scenario="supplier_slip",
        scrap=None,  # no WIP scrap clock in this scenario
        time=ttd,
        throughput_loss_units=0,
        total_at_risk_units=0,
        at_risk_orders=[],
        alt_line={"late_pos": late, "first_runout": first_negative},
    )


# =====================================================================
# Scenario 3: scrap trend — early detection value
# =====================================================================

def run_scenario_3_offline(
    agent_minutes: float = DEFAULT_AGENT_ESTIMATE_MINUTES,
    manual_minutes: float = DEFAULT_MANUAL_DELAY_MINUTES,
) -> ScenarioBaseline:
    """Scenario 3: scrap trend deterioration on DAL1/L1.

    The value here is catching a trend early. No immediate scrap clock,
    but the cost of delayed awareness compounds: every day the changeover
    rate stays elevated means more scrap.
    """
    # line_id=10 is DAL1/L1 in the generated dataset
    comp = production.compare_scrap_windows(line_id=10)["rows"]
    baseline = next((r for r in comp if r["bucket"] == "baseline"), {})
    recent = next((r for r in comp if r["bucket"] == "recent"), {})

    ttd = compute_time_to_decision(
        scenario="scrap_signal",
        agent_estimate_seconds=agent_minutes * 60,
        manual_minutes=manual_minutes,
    )

    # Estimate the daily scrap cost of the elevated rate
    baseline_pct = float(baseline.get("scrap_pct") or 0)
    recent_pct = float(recent.get("scrap_pct") or 0)
    lift_pct = recent_pct - baseline_pct

    return ScenarioBaseline(
        scenario="scrap_signal",
        scrap=None,
        time=ttd,
        throughput_loss_units=0,
        total_at_risk_units=0,
        at_risk_orders=[],
        alt_line={"baseline_pct": baseline_pct,
                  "recent_pct": recent_pct,
                  "lift_pp": round(lift_pct, 2)},
    )


# =====================================================================
# Live mode — run the agent and measure real time
# =====================================================================

async def run_scenario_1_live(
    manual_minutes: float = DEFAULT_MANUAL_DELAY_MINUTES,
) -> ScenarioBaseline:
    """Run the agent on Demo 1 and measure real wall time.

    Needs ANTHROPIC_API_KEY.
    """
    from proof.trace import TRACE_DIR, configure
    configure("baseline_live")

    from proof.agents.coordinator import ask

    # Run the agent on the Demo 1 question
    result = await ask(
        "Line 3 at TOR1 just went down — mechanical failure, sheeter gearbox "
        "seized. Maintenance says 90 minutes. What is at risk and what should "
        "I do?",
        verbose=True,
    )

    # Read the measured time from the trace
    trace_path = TRACE_DIR / "baseline_live.jsonl"
    ttd = compute_time_to_decision(
        scenario="line_down",
        trace_path=trace_path,
        manual_minutes=manual_minutes,
    )

    # Now compute the scrap-avoided with the measured agent time
    agent_min = ttd.agent_minutes or DEFAULT_AGENT_ESTIMATE_MINUTES
    baseline = run_scenario_1_offline(
        agent_minutes=agent_min,
        manual_minutes=manual_minutes,
    )
    # Replace the estimated TTD with the measured one
    baseline.time = ttd

    return baseline
