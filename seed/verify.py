"""
Sanity-check the generated plant.

This is not a test suite (that arrives in Phase 5). It is the thing you run
after `make seed` to confirm the dataset is plausible before you build agents
on top of it. Synthetic data that looks wrong here will look wrong in a demo,
and by then it is embarrassing rather than fixable.

It connects as `agent_read` on purpose: if any query here fails on
permissions, the least-privilege grants are wrong and every later phase
would inherit the bug.

Run:  make verify
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.db import connect, sim_now  # noqa: E402

CHECKS = [
    ("plants / lines / skus",
     "SELECT (SELECT count(*) FROM ops.plants), (SELECT count(*) FROM ops.lines), "
     "(SELECT count(*) FROM ops.skus)"),
    ("production runs",        "SELECT count(*) FROM ops.production_runs"),
    ("downtime events",        "SELECT count(*) FROM ops.downtime_events"),
    ("scrap events",           "SELECT count(*) FROM ops.scrap_events"),
    ("wip batches (staged)",
     "SELECT count(*) FROM ops.wip_batches WHERE status = 'staged'"),
    ("customer order lines",   "SELECT count(*) FROM scm.customer_order_lines"),
    ("open POs",
     "SELECT count(*) FROM scm.purchase_orders WHERE status = 'in_transit'"),
]


def main() -> None:
    with connect("agent_read") as conn, conn.cursor() as cur:
        now = sim_now(conn)
        print(f"sim now = {now.isoformat()}\n")

        print("row counts")
        for label, sql in CHECKS:
            cur.execute(sql)
            vals = cur.fetchone()
            print(f"  {label:24s} {', '.join(f'{v:,}' for v in vals)}")

        # --- plausibility: downtime should be a long-tailed distribution ---
        print("\ndowntime minutes by reason (median / p95 / count)")
        cur.execute("""
            SELECT reason_code,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_minutes),
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_minutes),
                   count(*)
            FROM ops.downtime_events
            WHERE duration_minutes IS NOT NULL
            GROUP BY reason_code ORDER BY count(*) DESC
        """)
        for reason, med, p95, n in cur.fetchall():
            # A flat median-to-p95 ratio would mean the durations are uniform,
            # i.e. invented. Real stoppage data is heavily right-skewed.
            print(f"  {reason:14s} {med:>4} /{p95:>5} / {n:>6,}")

        # --- plausibility: scrap rate should sit in single-digit percent ---
        print("\nscrap rate by category (last 30 days)")
        # Scrap is aggregated to one row per run BEFORE joining to runs.
        # Joining scrap_events directly to production_runs would count a run's
        # planned_units once per scrap row and silently deflate the rate.
        cur.execute("""
            WITH s AS (
                SELECT run_id, SUM(units) AS scrap_units
                FROM ops.scrap_events GROUP BY run_id
            )
            SELECT k.category,
                   round(SUM(COALESCE(s.scrap_units,0))::numeric
                         / NULLIF(SUM(r.planned_units),0) * 100, 2)
            FROM ops.production_runs r
            JOIN ops.skus k ON k.sku_id = r.sku_id
            LEFT JOIN s ON s.run_id = r.run_id
            WHERE r.planned_start > (SELECT now_ts FROM ops.sim_clock) - interval '30 days'
              AND r.planned_end < (SELECT now_ts FROM ops.sim_clock)
            GROUP BY k.category ORDER BY 2 DESC
        """)
        for cat, pct in cur.fetchall():
            print(f"  {cat:14s} {pct}%")

        # --- the Demo 1 situation, as the agent will find it ---
        print("\nopen downtime right now")
        cur.execute("""
            SELECT p.plant_code, l.line_code, d.reason_code, d.eta_minutes,
                   round(EXTRACT(EPOCH FROM (%s - d.started_at))/60) AS down_min
            FROM ops.downtime_events d
            JOIN ops.lines l ON l.line_id = d.line_id
            JOIN ops.plants p ON p.plant_id = l.plant_id
            WHERE d.ended_at IS NULL
            ORDER BY d.started_at
        """, (now,))
        rows = cur.fetchall()
        if not rows:
            print("  (none) -- run `make scenario`")
        for plant, line, reason, eta, down in rows:
            print(f"  {plant}/{line}  {reason}  down {down:.0f} min, ETA {eta} min")

        print("\nstaged WIP on stopped lines (the scrap clock)")
        cur.execute("""
            SELECT w.batch_code, k.name, w.units,
                   round(EXTRACT(EPOCH FROM (w.proof_expires_at - %s))/60) AS mins_left
            FROM ops.wip_batches w
            JOIN ops.skus k ON k.sku_id = w.sku_id
            WHERE w.status = 'staged'
              AND w.line_id IN (SELECT line_id FROM ops.downtime_events
                                WHERE ended_at IS NULL)
            ORDER BY w.proof_expires_at
        """, (now,))
        for code, name, units, mins in cur.fetchall():
            flag = "EXPIRES BEFORE RESTART" if mins is not None and mins < 90 else ""
            print(f"  {code}  {name:22s} {units:>6,} units  {mins:>4.0f} min left  {flag}")


if __name__ == "__main__":
    main()
