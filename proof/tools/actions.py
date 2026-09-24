"""Action-queue tools -- the only write path in the system.

Every function here connects as `action_rw`, which can INSERT a proposal and
nothing else. It holds no UPDATE on act.proposed_actions, so this module
structurally cannot approve; it holds no write grant anywhere in ops/scm/qms,
so it structurally cannot apply one either.

That is why `propose_*` is honest about what it did: it queued a request. The
agent is never in a position to claim something happened when it did not.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from proof.authz import Denied, check, record_denial
from proof.db import connect, sim_now
from proof.identity import Principal

from ._base import query

# How long a proposal stays actionable. Operations decisions are perishable:
# an approval for "move the run to L5" is worthless once the stoppage is over,
# and an approval screen full of stale items is one nobody reads.
TTL = {
    "reallocate_run": timedelta(hours=4),
    "notify_customer": timedelta(hours=8),
    "expedite_purchase_order": timedelta(days=2),
    "hold_product": timedelta(hours=12),
}


def _propose(principal: Principal, action_type: str, plant_code: str,
             rationale: str, payload: dict,
             source_question: str | None = None) -> dict[str, Any]:
    """Shared proposal path: authorize, then queue. Never execute."""
    capability = f"propose:{action_type}"
    try:
        check(principal, capability, plant_code)
    except Denied as denial:
        # Record under action_rw, which holds INSERT on authz_denials only.
        with connect("action_rw") as conn:
            record_denial(conn, principal, denial, sim_now(conn))
        return denial.as_tool_result()

    with connect("action_rw") as conn:
        now = sim_now(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO act.proposed_actions
                    (action_type, plant_code, proposed_by, proposed_at,
                     rationale, payload, source_question, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING action_id, expires_at
            """, (action_type, plant_code, principal.principal_id, now,
                  rationale, json.dumps(payload), source_question,
                  now + TTL[action_type]))
            action_id, expires_at = cur.fetchone()
            cur.execute("""
                INSERT INTO act.decision_log (action_id, at, principal_id, event, detail)
                VALUES (%s, %s, %s, 'proposed', %s)
            """, (action_id, now, principal.principal_id, rationale))
        conn.commit()

    approver = ("a regional director" if action_type == "notify_customer"
                else "the plant manager")
    return {
        "rows": [{
            "action_id": action_id,
            "action_type": action_type,
            "plant_code": plant_code,
            "status": "pending",
            "proposed_by": principal.principal_id,
            "expires_at": expires_at.isoformat(),
            "approved_by": None,
        }],
        "row_count": 1,
        "sql": "INSERT INTO act.proposed_actions (...) RETURNING action_id",
        "note": (f"QUEUED, NOT DONE. Action {action_id} is pending approval by "
                 f"{approver} and expires at {expires_at:%H:%M}. Nothing has "
                 f"changed in the plant. Tell the user it awaits approval -- "
                 f"do not describe it as completed or scheduled."),
    }


def propose_reallocate_run(principal: Principal, plant_code: str,
                           run_id: int, from_line: str, to_line: str,
                           units_at_risk: int, changeover_minutes: int,
                           rationale: str,
                           source_question: str | None = None) -> dict[str, Any]:
    """Queue a request to move a production run to a different line."""
    return _propose(principal, "reallocate_run", plant_code, rationale, {
        "run_id": run_id,
        "from_line": from_line,
        "to_line": to_line,
        "units_at_risk": units_at_risk,
        "changeover_minutes": changeover_minutes,
    }, source_question)


def propose_notify_customer(principal: Principal, plant_code: str,
                            order_code: str, customer: str,
                            shortfall_units: int, revised_availability: str,
                            draft_message: str, rationale: str,
                            source_question: str | None = None) -> dict[str, Any]:
    """Queue a draft short-shipment notification for commercial release.

    Never sends. POL-CUSTOMER-NOTIF 4.1 is explicit that plant staff do not
    contact customers directly, so the most this can ever do is put a draft
    in front of the account owner.
    """
    return _propose(principal, "notify_customer", plant_code, rationale, {
        "order_code": order_code,
        "customer": customer,
        "shortfall_units": shortfall_units,
        "revised_availability": revised_availability,
        "draft_message": draft_message,
    }, source_question)


def propose_expedite_po(principal: Principal, plant_code: str, po_code: str,
                        supplier: str, material_code: str,
                        requested_date: str, rationale: str,
                        source_question: str | None = None) -> dict[str, Any]:
    """Queue a request to expedite an inbound purchase order."""
    return _propose(principal, "expedite_purchase_order", plant_code, rationale, {
        "po_code": po_code,
        "supplier": supplier,
        "material_code": material_code,
        "requested_date": requested_date,
    }, source_question)


def propose_hold_product(principal: Principal, plant_code: str,
                         scope: str, reason: str, rationale: str,
                         source_question: str | None = None) -> dict[str, Any]:
    """Queue a quality hold.

    SOP-HOLD-RELEASE 2.1 says any employee may place a hold and no approval is
    needed to hold -- only to release. That is a human placing it, though. An
    agent-initiated hold stops a line and is queued like everything else.
    """
    return _propose(principal, "hold_product", plant_code, rationale, {
        "scope": scope,
        "reason": reason,
    }, source_question)


def list_pending_actions(plant_code: str | None = None) -> dict[str, Any]:
    """What is currently awaiting a human decision.

    Read-only, so it runs as agent_read like every other read tool.
    """
    return query("""
        SELECT a.action_id, a.action_type, a.plant_code, a.status,
               a.proposed_by, p.display_name AS proposed_by_name,
               a.proposed_at, a.rationale, a.payload, a.expires_at,
               (a.expires_at < (SELECT now_ts FROM ops.sim_clock)) AS is_expired
        FROM act.proposed_actions a
        JOIN act.principals p ON p.principal_id = a.proposed_by
        WHERE a.status = 'pending'
          AND (%s::text IS NULL OR a.plant_code = %s)
        ORDER BY a.expires_at
    """, (plant_code, plant_code),
        note="These are PROPOSALS awaiting human approval. None of them has "
             "taken effect. An expired proposal must be re-proposed, not "
             "approved.")


def get_action(action_id: int) -> dict[str, Any]:
    """One action with its full decision history."""
    return query("""
        SELECT a.action_id, a.action_type, a.plant_code, a.status,
               a.proposed_by, a.proposed_at, a.rationale, a.payload,
               a.decided_by, a.decided_at, a.decision_note, a.executed_at,
               a.expires_at, a.source_question,
               (SELECT json_agg(json_build_object(
                          'at', l.at, 'principal', l.principal_id,
                          'event', l.event, 'detail', l.detail) ORDER BY l.at)
                FROM act.decision_log l WHERE l.action_id = a.action_id) AS history
        FROM act.proposed_actions a
        WHERE a.action_id = %s
    """, (action_id,))
