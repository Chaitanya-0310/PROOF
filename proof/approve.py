"""
The human approval CLI.

    proof-approve list
    proof-approve show 3
    proof-approve approve 3 --as j.okafor --note "agreed, L5 is free"
    proof-approve reject 3 --as j.okafor --note "L5 needed for the 06:00 run"

Deliberately NOT part of the agent. It is a separate program, run by a
person, connecting as a separate database role (`approver`) that is the only
role in the system holding UPDATE on act.proposed_actions.

So "the agent approved its own proposal" is not a bug that a prompt change
can introduce. It would require a new database grant.

The approval screen shows the rationale, the structured payload and the
original question verbatim. An approver who cannot see what was actually
asked is rubber-stamping, and a rubber stamp is worse than no gate at all --
it produces an audit trail that looks like oversight.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.authz import Denied, check  # noqa: E402
from proof.db import connect, load_env, sim_now  # noqa: E402
from proof.identity import DEMO_PRINCIPALS, resolve  # noqa: E402

load_env()


def cmd_list(args) -> int:
    with connect("approver") as conn, conn.cursor() as cur:
        now = sim_now(conn)
        cur.execute("""
            SELECT a.action_id, a.action_type, a.plant_code, a.proposed_by,
                   a.proposed_at, a.rationale, a.expires_at,
                   (a.expires_at < %s) AS expired
            FROM act.proposed_actions a
            WHERE a.status = 'pending'
              AND (%s::text IS NULL OR a.plant_code = %s)
            ORDER BY a.expires_at
        """, (now, args.plant, args.plant))
        rows = cur.fetchall()

    if not rows:
        print("nothing pending.")
        return 0
    print(f"{len(rows)} pending  (sim now {now:%Y-%m-%d %H:%M})\n")
    for aid, kind, plant, by, at, why, exp, expired in rows:
        mins = (exp - now).total_seconds() / 60
        flag = "EXPIRED" if expired else f"{mins:.0f}m left"
        print(f"  [{aid}] {kind:24s} {plant}  by {by:10s}  {flag}")
        print(f"       {why}")
    print("\nproof-approve show <id>   for the full proposal")
    return 0


def cmd_show(args) -> int:
    with connect("approver") as conn, conn.cursor() as cur:
        now = sim_now(conn)
        cur.execute("""
            SELECT action_id, action_type, plant_code, status, proposed_by,
                   proposed_at, rationale, payload, expires_at, source_question,
                   decided_by, decided_at, decision_note
            FROM act.proposed_actions WHERE action_id = %s
        """, (args.action_id,))
        row = cur.fetchone()
        if row is None:
            print(f"no action {args.action_id}", file=sys.stderr)
            return 1
        cur.execute("""
            SELECT at, principal_id, event, detail FROM act.decision_log
            WHERE action_id = %s ORDER BY at
        """, (args.action_id,))
        history = cur.fetchall()

    (aid, kind, plant, status, by, at, why, payload, exp, question,
     dby, dat, dnote) = row
    print(f"action {aid}  {kind}  [{status}]")
    print(f"  plant      {plant}")
    print(f"  proposed   {by} at {at:%Y-%m-%d %H:%M}")
    print(f"  expires    {exp:%Y-%m-%d %H:%M}"
          f"{'  (EXPIRED)' if exp < now else ''}")
    print(f"  rationale  {why}")
    if question:
        # Verbatim, never summarised: an approver needs to see what was
        # actually asked, not the agent's reading of it.
        print(f"  asked      \"{question}\"")
    print("  payload")
    for k, v in (payload or {}).items():
        print(f"    {k:22s} {v}")
    if dby:
        print(f"  decided    {dby} at {dat:%Y-%m-%d %H:%M} -- {dnote or ''}")
    if history:
        print("  history")
        for h_at, who, event, detail in history:
            print(f"    {h_at:%H:%M}  {event:9s} {who:12s} {detail or ''}")
    return 0


def _decide(args, event: str) -> int:
    try:
        principal = resolve(args.as_principal)
    except Exception as exc:  # noqa: BLE001
        print(f"{exc}", file=sys.stderr)
        return 1

    with connect("approver") as conn:
        now = sim_now(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT action_type, plant_code, status, expires_at, proposed_by
                FROM act.proposed_actions WHERE action_id = %s
            """, (args.action_id,))
            row = cur.fetchone()
            if row is None:
                print(f"no action {args.action_id}", file=sys.stderr)
                return 1
            kind, plant, status, expires_at, proposed_by = row

            if status != "pending":
                print(f"action {args.action_id} is already {status}",
                      file=sys.stderr)
                return 1

            # An expired proposal is re-proposed, never approved. Operations
            # decisions are about a moment, and the moment has passed.
            if expires_at < now:
                cur.execute("""
                    UPDATE act.proposed_actions SET status = 'expired'
                    WHERE action_id = %s
                """, (args.action_id,))
                cur.execute("""
                    INSERT INTO act.decision_log (action_id, at, principal_id, event, detail)
                    VALUES (%s, %s, %s, 'expired', 'expired before a decision')
                """, (args.action_id, now, principal.principal_id))
                conn.commit()
                print(f"action {args.action_id} expired at "
                      f"{expires_at:%H:%M} and was not approved.",
                      file=sys.stderr)
                return 1

            # Same authorization layer the agent goes through. The approval
            # CLI is not privileged by being a CLI.
            if event == "approved":
                try:
                    check(principal, f"approve:{kind}", plant)
                except Denied as denial:
                    print(f"DENIED: {denial}", file=sys.stderr)
                    return 1

                # Self-approval is refused even where the role permits the
                # action, because a gate one person can walk through alone is
                # not a gate. SOP-LINE-REALLOC 3.1.
                if proposed_by == principal.principal_id:
                    print(f"DENIED: {principal.principal_id} proposed this "
                          f"action and cannot approve it (SOP-LINE-REALLOC 3.1: "
                          f"the requester may not approve their own request).",
                          file=sys.stderr)
                    return 1

            cur.execute("""
                UPDATE act.proposed_actions
                SET status = %s, decided_by = %s, decided_at = %s,
                    decision_note = %s
                WHERE action_id = %s
            """, (event, principal.principal_id, now, args.note, args.action_id))
            cur.execute("""
                INSERT INTO act.decision_log (action_id, at, principal_id, event, detail)
                VALUES (%s, %s, %s, %s, %s)
            """, (args.action_id, now, principal.principal_id, event, args.note))
        conn.commit()

    print(f"action {args.action_id} {event} by {principal.principal_id} "
          f"({principal.role}).")
    if event == "approved":
        # Approval and execution are different steps on purpose. Phase 5
        # gives the executor its own process and its own trace.
        print("  Approved, NOT yet executed. Execution is a separate step.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="proof-approve", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="pending actions")
    pl.add_argument("--plant", default=None)
    pl.set_defaults(fn=cmd_list)

    ps = sub.add_parser("show", help="one action in full")
    ps.add_argument("action_id", type=int)
    ps.set_defaults(fn=cmd_show)

    for name in ("approve", "reject"):
        pd = sub.add_parser(name, help=f"{name} a pending action")
        pd.add_argument("action_id", type=int)
        pd.add_argument("--as", dest="as_principal", required=True,
                        help=f"who is deciding ({', '.join(DEMO_PRINCIPALS)})")
        pd.add_argument("--note", default=None, help="reason for the decision")
        pd.set_defaults(fn=lambda a, _e=name + "d": _decide(a, _e))

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
