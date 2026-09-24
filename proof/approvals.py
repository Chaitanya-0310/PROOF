"""
The approval decision, in one place.

Both the human CLI (`proof/approve.py`) and the web API call `decide()`, so
there is exactly one path that can move an action from pending to
approved/rejected -- and it runs the SAME authorization checks whether the
decision comes from a terminal or a browser. A second decision path is a
second place for the rules to drift; there isn't one.

`decide()` connects as the `approver` role, the only role holding UPDATE on
act.proposed_actions. The web server importing this does not thereby gain the
ability to approve from anywhere else -- the grant is on the role, and only
this function uses it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .authz import Denied, check
from .db import connect, sim_now
from .identity import Principal, resolve


@dataclass
class Decision:
    ok: bool
    status: str          # approved | rejected | denied | expired | error | not_found
    message: str
    action_id: int | None = None


def decide(action_id: int, principal_id: str, event: str,
           note: str | None = None) -> Decision:
    """Approve or reject one pending action as `principal_id`.

    `event` is 'approved' or 'rejected'. Every guard that protects the CLI
    protects the API through this one function: role capability, plant scope,
    the no-self-approval rule, and expiry.
    """
    if event not in ("approved", "rejected"):
        return Decision(False, "error", f"bad event {event!r}")

    try:
        principal: Principal = resolve(principal_id)
    except Exception as exc:  # noqa: BLE001
        return Decision(False, "error", str(exc))

    with connect("approver") as conn:
        now = sim_now(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT action_type, plant_code, status, expires_at, proposed_by
                FROM act.proposed_actions WHERE action_id = %s
            """, (action_id,))
            row = cur.fetchone()
            if row is None:
                return Decision(False, "not_found", f"no action {action_id}",
                                action_id)
            kind, plant, status, expires_at, proposed_by = row

            if status != "pending":
                return Decision(False, "error",
                                f"action {action_id} is already {status}",
                                action_id)

            # An expired proposal is re-proposed, never approved -- the moment
            # it was about has passed.
            if expires_at < now:
                cur.execute("UPDATE act.proposed_actions SET status='expired' "
                            "WHERE action_id=%s", (action_id,))
                cur.execute("""
                    INSERT INTO act.decision_log
                        (action_id, at, principal_id, event, detail)
                    VALUES (%s, %s, %s, 'expired', 'expired before a decision')
                """, (action_id, now, principal.principal_id))
                conn.commit()
                return Decision(False, "expired",
                                f"action {action_id} expired at "
                                f"{expires_at:%H:%M} and was not approved",
                                action_id)

            if event == "approved":
                # Same authorization layer the agent goes through. Being an
                # approval UI does not grant privilege.
                try:
                    check(principal, f"approve:{kind}", plant)
                except Denied as denial:
                    return Decision(False, "denied", str(denial), action_id)

                # A gate one person walks through alone is not a gate.
                if proposed_by == principal.principal_id:
                    return Decision(
                        False, "denied",
                        f"{principal.principal_id} proposed this action and "
                        f"cannot approve it (SOP-LINE-REALLOC 3.1)", action_id)

            cur.execute("""
                UPDATE act.proposed_actions
                SET status=%s, decided_by=%s, decided_at=%s, decision_note=%s
                WHERE action_id=%s
            """, (event, principal.principal_id, now, note, action_id))
            cur.execute("""
                INSERT INTO act.decision_log
                    (action_id, at, principal_id, event, detail)
                VALUES (%s, %s, %s, %s, %s)
            """, (action_id, now, principal.principal_id, event, note))
        conn.commit()

    verb = "approved" if event == "approved" else "rejected"
    tail = (" Approved, not yet executed -- execution is a separate step."
            if event == "approved" else "")
    return Decision(True, event,
                    f"action {action_id} {verb} by {principal.principal_id} "
                    f"({principal.role}).{tail}", action_id)
