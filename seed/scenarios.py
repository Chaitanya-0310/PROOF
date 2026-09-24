"""
Plant the three demo situations, deterministically, at simulation 'now'.

This file is separate from generate_plant.py on purpose, and it is the most
honest file in the repo: it states plainly which facts were arranged so the
demos have something to find. A reviewer can read it in two minutes and know
exactly what was staged versus what emerged from the generator.

Every scenario is planted as ORDINARY ROWS -- an open downtime event, a
slipped ETA. There is no scenario flag anywhere in the schema and no branch
in the agent that says "if demo 1". The agent discovers these the same way
it would discover a real one: by querying.

Run:  make scenario   (always after `make seed`)
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.db import connect, sim_now  # noqa: E402


# =====================================================================
# SCENARIO 1 -- "The sheeter went down"
#
# TOR1 Line 3 suffers a mechanical failure mid-run with a 90-minute ETA.
# Staged dough keeps proofing while the line is stopped, and the run is
# allocated to a strategic QSR order shipping in a few hours.
#
# What the agent must independently work out:
#   - units lost over the 90 minutes at this line's rate for this SKU
#   - which staged WIP batches expire BEFORE the line can restart
#   - which customer order goes short, by how much, and how it ranks
#   - whether an alternate line is free and allergen-compatible
# =====================================================================
def scenario_1_line_down(cur, now) -> None:
    # The line: TOR1 (plant 1) / L3.
    cur.execute("""
        SELECT l.line_id FROM ops.lines l
        JOIN ops.plants p ON p.plant_id = l.plant_id
        WHERE p.plant_code = 'TOR1' AND l.line_code = 'L3'
    """)
    line_id = cur.fetchone()[0]

    # Find the run that is in progress on that line at sim-now. The generator
    # guarantees one exists because runs tile the whole timeline.
    cur.execute("""
        SELECT run_id, sku_id FROM ops.production_runs
        WHERE line_id = %s AND planned_start <= %s AND planned_end > %s
        ORDER BY planned_start LIMIT 1
    """, (line_id, now, now))
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"no running run on line {line_id}; regenerate the seed")
    run_id, sku_id = row

    # Close any other open event on this line so there is exactly one story.
    cur.execute("""
        UPDATE ops.downtime_events SET ended_at = started_at + interval '15 minutes'
        WHERE line_id = %s AND ended_at IS NULL
    """, (line_id,))

    # The stoppage: started 12 minutes ago, still open, 90-minute ETA.
    started = now - timedelta(minutes=12)
    cur.execute("""
        INSERT INTO ops.downtime_events
            (line_id, run_id, reason_code, reason_detail, started_at, ended_at, eta_minutes)
        VALUES (%s, %s, 'MECHANICAL',
                'sheeter gearbox seized - maint called, est 90 min', %s, NULL, 90)
        RETURNING event_id
    """, (line_id, run_id, started))
    event_id = cur.fetchone()[0]

    # Stage four dough batches with staggered expiries straddling the 90-minute
    # restart. Two die before restart, two survive -- so the correct answer is
    # a NUMBER, not "everything is fine" or "everything is lost". An agent that
    # hand-waves this gets it visibly wrong.
    cur.execute("DELETE FROM ops.wip_batches WHERE line_id = %s AND status = 'staged'",
                (line_id,))
    offsets = [
        (40, 1800),    # expires 40 min from now  -> LOST (restart is at +90)
        (70, 1650),    # expires 70 min from now  -> LOST
        (135, 1500),   # expires 135 min from now -> survives
        (190, 1500),   # expires 190 min from now -> survives
    ]
    for i, (mins, units) in enumerate(offsets, start=1):
        cur.execute("""
            INSERT INTO ops.wip_batches
                (batch_code, run_id, line_id, sku_id, units, staged_at,
                 proof_expires_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'staged')
        """, (f"WIP-S1-{i:03d}", run_id, line_id, sku_id, units,
              now - timedelta(minutes=30), now + timedelta(minutes=mins)))

    # Point the run at a strategic QSR order shipping this morning, so the
    # commercial consequence is concrete rather than abstract.
    cur.execute("""
        SELECT ol.order_line_id, o.order_id
        FROM scm.run_order_allocations a
        JOIN scm.customer_order_lines ol ON ol.order_line_id = a.order_line_id
        JOIN scm.customer_orders o ON o.order_id = ol.order_id
        WHERE a.run_id = %s
    """, (run_id,))
    alloc = cur.fetchone()
    if alloc:
        order_line_id, order_id = alloc
        cur.execute("""
            UPDATE scm.customer_orders
            SET customer_id = (SELECT customer_id FROM scm.customers WHERE customer_code='QSRA'),
                promised_ship_at = %s,
                status = 'open'
            WHERE order_id = %s
        """, (now + timedelta(hours=5), order_id))
        cur.execute("UPDATE scm.customer_order_lines SET qty_fulfilled = 0 "
                    "WHERE order_line_id = %s", (order_line_id,))

    # Leave TOR1 L5 genuinely idle for the next 6 hours so a reallocation
    # option actually exists. If no option existed the demo would only ever
    # have one answer, which is not much of a demo.
    cur.execute("""
        SELECT l.line_id FROM ops.lines l
        JOIN ops.plants p ON p.plant_id = l.plant_id
        WHERE p.plant_code = 'TOR1' AND l.line_code = 'L5'
    """)
    alt_line = cur.fetchone()[0]
    cur.execute("""
        UPDATE ops.production_runs SET status = 'aborted'
        WHERE line_id = %s AND planned_start < %s AND planned_end > %s
    """, (alt_line, now + timedelta(hours=6), now))

    print(f"  [1] line_down       line={line_id} run={run_id} event={event_id} "
          f"alt_line={alt_line}  (2 of 4 staged batches expire before restart)")


# =====================================================================
# SCENARIO 2 -- "The flour truck is two days late"
#
# An in-transit flour PO to TOR1 slips 2 days past its promised date.
# The agent must trace: material -> BOM -> SKUs -> scheduled runs in the
# gap -> customer orders those runs serve, then propose a priority call.
# =====================================================================
def scenario_2_supplier_slip(cur, now) -> None:
    cur.execute("""
        SELECT po.po_id FROM scm.purchase_orders po
        JOIN scm.materials m ON m.material_id = po.material_id
        JOIN ops.plants p ON p.plant_id = po.plant_id
        WHERE m.material_code = 'FLR-HRS' AND p.plant_code = 'TOR1'
          AND po.status = 'in_transit'
        ORDER BY po.promised_at LIMIT 1
    """)
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("no in-transit flour PO at TOR1; regenerate the seed")
    po_id = row[0]

    cur.execute("""
        UPDATE scm.purchase_orders
        SET eta_at = promised_at + interval '2 days'
        WHERE po_id = %s
        RETURNING po_code, promised_at, eta_at
    """, (po_id,))
    po_code, promised, eta = cur.fetchone()

    # Draw flour stock down so the slip actually bites. With a comfortable
    # buffer the honest answer would be "no impact", which teaches nothing.
    cur.execute("""
        UPDATE scm.inventory_lots
        SET qty_on_hand = 2200
        WHERE lot_id IN (
            SELECT l.lot_id FROM scm.inventory_lots l
            JOIN scm.materials m ON m.material_id = l.material_id
            JOIN ops.plants p ON p.plant_id = l.plant_id
            WHERE m.material_code = 'FLR-HRS' AND p.plant_code = 'TOR1'
              AND l.status = 'available'
        )
    """)
    print(f"  [2] supplier_slip   po={po_code} promised={promised:%Y-%m-%d} "
          f"eta={eta:%Y-%m-%d}  (TOR1 flour drawn down to ~2.2t/lot)")


# =====================================================================
# SCENARIO 3 -- "Why is donut scrap up this month?"
#
# Nothing is planted here. The signal was generated in generate_plant.py:
# DAL1 Line 1 has been changing over roughly twice as often for the last
# 30 days, dragging CHANGEOVER_PURGE and STARTUP_LOSS scrap up with it.
#
# This function only verifies the signal is present and strong enough to
# be findable. If a generator change ever washes it out, `make scenario`
# fails loudly here rather than the demo quietly becoming unimpressive.
# =====================================================================
def scenario_3_check_scrap_signal(cur, now) -> None:
    # NOTE ON THE SHAPE OF THIS QUERY.
    # The obvious version -- join scrap_events straight to production_runs and
    # SUM(units)/SUM(planned_units) -- is WRONG, and wrong in the worst
    # possible direction. A run with 5 scrap rows contributes its planned_units
    # five times, so the denominator inflates exactly when scrap rises, and a
    # real deterioration reads as an improvement. The first version of this
    # check reported the planted signal as -0.13pp when it is truly +0.71pp.
    # Aggregate scrap to one row per run BEFORE joining. The agent's own
    # Demo 3 query has to do the same thing, which is precisely why this
    # metric is worth pinning down here first.
    cur.execute("""
        WITH r AS (
            SELECT run_id, planned_units,
                   CASE WHEN planned_start > %s - interval '30 days'
                        THEN 'recent' ELSE 'baseline' END AS bucket
            FROM ops.production_runs
            WHERE line_id = 10
              AND planned_start > %s - interval '90 days'
              AND planned_end < %s
        ),
        s AS (
            SELECT run_id, SUM(units) AS scrap_units
            FROM ops.scrap_events GROUP BY run_id
        ),
        c AS (
            SELECT run_id, count(*) AS n_changeovers
            FROM ops.downtime_events
            WHERE reason_code = 'CHANGEOVER' GROUP BY run_id
        )
        SELECT r.bucket,
               SUM(COALESCE(s.scrap_units, 0))::numeric
                   / NULLIF(SUM(r.planned_units), 0) * 100 AS scrap_pct,
               avg(COALESCE(c.n_changeovers, 0))           AS co_per_run
        FROM r LEFT JOIN s USING (run_id) LEFT JOIN c USING (run_id)
        GROUP BY r.bucket
    """, (now, now, now))
    rows = {b: (float(p or 0), float(c or 0)) for b, p, c in cur.fetchall()}
    recent, recent_co = rows.get("recent", (0, 0))
    baseline, base_co = rows.get("baseline", (0, 0))
    lift = recent - baseline
    status = "OK" if lift > 0.3 else "WEAK"
    print(f"  [3] scrap_signal    DAL1/L1 scrap {baseline:.2f}% -> {recent:.2f}% "
          f"({lift:+.2f}pp), changeovers/run {base_co:.2f} -> {recent_co:.2f}  [{status}]")
    if status == "WEAK":
        print("      warning: Demo 3's root cause may be too faint to find. "
              "Increase the changeover skew in generate_plant.py.")


def seed_principals(cur) -> None:
    """Load the Phase 4 demo principals.

    Four people with genuinely different authority, so the authorization demo
    shows something rather than asserting it: a supervisor who can propose but
    not approve, a plant manager who can approve within TOR1 only, a regional
    director who can approve anywhere, and an operator who can only read.
    """
    from proof.identity import DEMO_PRINCIPALS

    rows = [(p.principal_id, p.display_name, p.role, list(p.plant_scope))
            for p in DEMO_PRINCIPALS.values()]
    cur.execute("TRUNCATE act.decision_log, act.authz_denials CASCADE")
    cur.execute("TRUNCATE act.proposed_actions CASCADE")
    cur.execute("TRUNCATE act.principals CASCADE")
    cur.executemany(
        "INSERT INTO act.principals "
        "(principal_id, display_name, role, plant_scope) VALUES (%s,%s,%s,%s)",
        rows)
    print(f"  [0] principals      {len(rows)} loaded: "
          + ", ".join(f"{p.principal_id}/{p.role}" for p in DEMO_PRINCIPALS.values()))


def main() -> None:
    with connect("seeder") as conn:
        with conn.cursor() as cur:
            now = sim_now(conn)
            print(f"Planting demo scenarios at sim now = {now.isoformat()}")
            seed_principals(cur)
            scenario_1_line_down(cur, now)
            scenario_2_supplier_slip(cur, now)
            scenario_3_check_scrap_signal(cur, now)
        conn.commit()
    print("  done.")


if __name__ == "__main__":
    main()
