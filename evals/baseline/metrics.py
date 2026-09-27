"""
Phase 6 metrics: scrap avoided and time-to-decision.

Both metrics rest on the same insight: PROOF surfaces a stoppage's full
consequences in ~2 minutes, while the manual process takes 30–45 minutes of
phone calls. That gap determines how much WIP is discovered in time to
salvage, which is the dollar value of the system.

SCRAP AVOIDED

For each stopped line, the harness:

  1. Reads the staged WIP batches and their proof-window expiries.
  2. Reads the best alternate line and its changeover cost.
  3. For each batch, computes a salvage deadline: the latest moment a
     reallocation decision can begin and still get the alternate line
     through changeover before the batch over-proofs.
  4. Under PROOF, awareness arrives at `agent_minutes`. Under the manual
     process, at `manual_minutes`. A batch where the agent's awareness
     falls before the salvage deadline but the manual process's does not
     is scrap the system saved.

The number is conservative: it assumes a single alternate line, does not
credit a faster changeover on a second line, and counts only batches that
die on the *current* line (not batches that could have been rescheduled
entirely). Honest limits are stated in the report.

TIME-TO-DECISION

Wall-clock seconds from the session span's start to its end, read from the
OTel JSONL trace file. Offline runs parameterise this; live runs measure it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# =====================================================================
# Data models
# =====================================================================

@dataclass
class WipBatchFate:
    """One staged batch and what happens to it under each awareness scenario."""
    batch_code: str
    units: int
    minutes_until_expiry: int
    survives_restart: bool
    salvage_deadline_minutes: float
    # Does awareness arrive before the salvage deadline?
    agent_can_salvage: bool
    manual_can_salvage: bool

    @property
    def saved_by_proof(self) -> bool:
        """True if PROOF saves this batch but the manual process does not."""
        return (not self.survives_restart
                and self.agent_can_salvage
                and not self.manual_can_salvage)


@dataclass
class ScrapAvoidedResult:
    """The scrap that PROOF surfaces in time to act on, but manual does not."""
    scenario: str
    plant_code: str
    line_code: str

    batches_total: int
    batches_expire_before_restart: int
    batches_saved_by_proof: int

    units_expiring: int           # total WIP units lost to the stoppage
    units_saved_by_proof: int     # the subset saved by faster awareness

    changeover_minutes: int       # to the best alternate line
    alt_line_code: str
    restart_eta_minutes: int       # operator's ETA for line restart

    agent_awareness_minutes: float
    manual_awareness_minutes: float

    batch_detail: list[WipBatchFate] = field(default_factory=list)

    @property
    def units_saved_pct(self) -> float:
        """Percentage of expiring WIP saved by PROOF."""
        if self.units_expiring == 0:
            return 0.0
        return 100.0 * self.units_saved_by_proof / self.units_expiring


@dataclass
class TimeToDecisionResult:
    """How long from question to complete answer."""
    scenario: str
    agent_seconds: float | None    # from trace, None if offline
    manual_seconds: float          # parameterised
    estimated: bool                # True if agent_seconds is parameterised

    @property
    def agent_minutes(self) -> float | None:
        return self.agent_seconds / 60 if self.agent_seconds else None

    @property
    def manual_minutes(self) -> float:
        return self.manual_seconds / 60

    @property
    def speedup(self) -> float | None:
        if self.agent_seconds and self.agent_seconds > 0:
            return self.manual_seconds / self.agent_seconds
        return None


@dataclass
class ScenarioBaseline:
    """Combined metrics for one scenario."""
    scenario: str
    scrap: ScrapAvoidedResult | None
    time: TimeToDecisionResult
    throughput_loss_units: int     # output not produced during downtime
    total_at_risk_units: int       # throughput loss + WIP scrap
    at_risk_orders: list[dict] = field(default_factory=list)
    alt_line: dict | None = None


# =====================================================================
# Computation — scrap avoided
# =====================================================================

def compute_scrap_avoided(
    wip_rows: list[dict],
    restart_minutes: int,
    changeover_minutes: int,
    alt_line_code: str,
    plant_code: str,
    line_code: str,
    scenario: str,
    agent_awareness_minutes: float = 2.0,
    manual_awareness_minutes: float = 35.0,
) -> ScrapAvoidedResult:
    """From WIP batch data and awareness delays, compute what PROOF saves.

    Parameters
    ----------
    wip_rows : list[dict]
        Rows from `project_wip_expiry`, each having at minimum
        `batch_code`, `units`, `minutes_until_expiry`,
        `expires_before_restart`.
    restart_minutes : int
        The operator's ETA for the line restart.
    changeover_minutes : int
        Minutes to change over the best alternate line.
    alt_line_code : str
        The line code of the best alternate.
    agent_awareness_minutes : float
        How soon PROOF has the answer. Default 2 min (typical measured).
    manual_awareness_minutes : float
        How soon the phone-tree process has the answer. Default 35 min.
    """
    fates: list[WipBatchFate] = []
    for row in wip_rows:
        expiry_min = row["minutes_until_expiry"]
        survives = not row["expires_before_restart"]

        # Salvage deadline: the latest moment someone can START the changeover
        # and still have the alternate line ready before this batch dies.
        # If negative, the batch is already unsaveable even with instant
        # awareness (its proof window is shorter than the changeover).
        salvage_deadline = expiry_min - changeover_minutes

        agent_saves = (not survives
                       and salvage_deadline >= agent_awareness_minutes)
        manual_saves = (not survives
                        and salvage_deadline >= manual_awareness_minutes)

        fates.append(WipBatchFate(
            batch_code=row["batch_code"],
            units=row["units"],
            minutes_until_expiry=expiry_min,
            survives_restart=survives,
            salvage_deadline_minutes=salvage_deadline,
            agent_can_salvage=agent_saves,
            manual_can_salvage=manual_saves,
        ))

    expiring = [f for f in fates if not f.survives_restart]
    saved = [f for f in fates if f.saved_by_proof]

    return ScrapAvoidedResult(
        scenario=scenario,
        plant_code=plant_code,
        line_code=line_code,
        batches_total=len(fates),
        batches_expire_before_restart=len(expiring),
        batches_saved_by_proof=len(saved),
        units_expiring=sum(f.units for f in expiring),
        units_saved_by_proof=sum(f.units for f in saved),
        changeover_minutes=changeover_minutes,
        alt_line_code=alt_line_code,
        restart_eta_minutes=restart_minutes,
        agent_awareness_minutes=agent_awareness_minutes,
        manual_awareness_minutes=manual_awareness_minutes,
        batch_detail=fates,
    )


# =====================================================================
# Computation — time-to-decision from OTel trace
# =====================================================================

def read_session_duration_from_trace(trace_path: Path) -> float | None:
    """Read the session span duration from a JSONL trace file.

    Returns seconds, or None if no session span is found.
    """
    for line in trace_path.read_text().splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        attrs = obj.get("attributes", {})
        if attrs.get("proof.kind") == "session":
            return obj.get("duration_ms", 0) / 1000.0
    return None


def compute_time_to_decision(
    scenario: str,
    trace_path: Path | None = None,
    agent_estimate_seconds: float | None = None,
    manual_minutes: float = 35.0,
) -> TimeToDecisionResult:
    """Build a time-to-decision result.

    If a trace file is given, the agent's time is measured from it.
    Otherwise falls back to the parameterised estimate.
    """
    measured = None
    if trace_path and trace_path.exists():
        measured = read_session_duration_from_trace(trace_path)

    agent_sec = measured or agent_estimate_seconds
    estimated = measured is None

    return TimeToDecisionResult(
        scenario=scenario,
        agent_seconds=agent_sec,
        manual_seconds=manual_minutes * 60,
        estimated=estimated,
    )
