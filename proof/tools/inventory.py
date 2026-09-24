"""Inventory domain tools -- WIP on the floor and raw material in the warehouse.

Stands in for WMS plus the MES WIP tracker. Contains `project_wip_expiry`,
which is the single most important tool in the project: it is the one that
turns a stoppage from an output problem into a perishability problem.
"""
from __future__ import annotations

from ._base import query, sim_now_sql

NOW = sim_now_sql()


def get_staged_wip(line_id: int) -> dict:
    """Proofed dough currently staged on a line, ordered by how soon it dies."""
    return query(f"""
        SELECT w.batch_code, w.batch_id, w.units,
               k.sku_code, k.name AS sku_name, k.category,
               w.staged_at, w.proof_expires_at,
               round(EXTRACT(EPOCH FROM (w.proof_expires_at - {NOW})) / 60)::int
                   AS minutes_until_expiry
        FROM ops.wip_batches w
        JOIN ops.skus k ON k.sku_id = w.sku_id
        WHERE w.line_id = %s AND w.status = 'staged'
        ORDER BY w.proof_expires_at
    """, (line_id,),
        note="A negative minutes_until_expiry means the batch is already past "
             "its proof window and is scrap now.")


def project_wip_expiry(line_id: int, restart_in_minutes: int) -> dict:
    """THE SCRAP CLOCK.

    Given how long until the line restarts, split the staged dough into what
    survives and what is lost. This is the question a supervisor cannot answer
    from a dashboard, because it needs a restart estimate that only exists in
    someone's head, combined with expiry timestamps that live in a different
    system.

    A batch is lost when it expires BEFORE the line can run again. Batches
    that survive are not free either -- they still have to be baked promptly,
    which is why `minutes_of_slack_after_restart` is returned rather than a
    bare survives/lost flag.
    """
    return query(f"""
        SELECT w.batch_code, w.units, k.sku_code, k.name AS sku_name,
               w.proof_expires_at,
               round(EXTRACT(EPOCH FROM (w.proof_expires_at - {NOW})) / 60)::int
                   AS minutes_until_expiry,
               %s AS restart_in_minutes,
               (w.proof_expires_at < {NOW} + make_interval(mins => %s))
                   AS expires_before_restart,
               round(EXTRACT(EPOCH FROM (w.proof_expires_at - {NOW})) / 60
                     - %s)::int AS minutes_of_slack_after_restart
        FROM ops.wip_batches w
        JOIN ops.skus k ON k.sku_id = w.sku_id
        WHERE w.line_id = %s AND w.status = 'staged'
        ORDER BY w.proof_expires_at
    """, (restart_in_minutes, restart_in_minutes, restart_in_minutes, line_id),
        note="Sum `units` where expires_before_restart is true to get units "
             "scrapped by the stoppage. This is SEPARATE from, and additional "
             "to, the output not produced while the line is down.")


def get_material_stock(plant_code: str, material_code: str | None = None) -> dict:
    """Raw material on hand at a plant, by lot."""
    return query(f"""
        SELECT p.plant_code, m.material_code, m.name AS material_name, m.uom,
               count(*)                       AS lots,
               SUM(i.qty_on_hand)             AS qty_available,
               SUM(i.qty_on_hand) FILTER (WHERE i.status = 'held') AS qty_held,
               min(i.expires_at)              AS earliest_lot_expiry
        FROM scm.inventory_lots i
        JOIN scm.materials m ON m.material_id = i.material_id
        JOIN ops.plants p    ON p.plant_id = i.plant_id
        WHERE p.plant_code = %s
          AND (%s::text IS NULL OR m.material_code = %s)
          AND i.status <> 'consumed'
        GROUP BY p.plant_code, m.material_code, m.name, m.uom
        ORDER BY m.material_code
    """, (plant_code, material_code, material_code),
        note="qty_available includes held lots. Subtract qty_held for what is "
             "genuinely usable.")


def get_open_purchase_orders(plant_code: str,
                             material_code: str | None = None) -> dict:
    """Inbound POs, with the gap between the contracted date and today's ETA.

    `days_late` is the slip. It is computed here rather than left to the model
    because date arithmetic is exactly the kind of thing an LLM gets subtly
    wrong, and the whole answer hangs off it.
    """
    return query(f"""
        SELECT po.po_code, s.name AS supplier, m.material_code,
               m.name AS material_name, po.qty, po.uom,
               po.promised_at, po.eta_at, po.status,
               round(EXTRACT(EPOCH FROM (po.eta_at - po.promised_at)) / 86400)::int
                   AS days_late,
               round(EXTRACT(EPOCH FROM (po.eta_at - {NOW})) / 86400, 1)
                   AS days_until_arrival
        FROM scm.purchase_orders po
        JOIN scm.suppliers s ON s.supplier_id = po.supplier_id
        JOIN scm.materials m ON m.material_id = po.material_id
        JOIN ops.plants p    ON p.plant_id = po.plant_id
        WHERE p.plant_code = %s
          AND po.status IN ('open', 'in_transit')
          AND (%s::text IS NULL OR m.material_code = %s)
        ORDER BY po.eta_at
    """, (plant_code, material_code, material_code),
        note="days_late > 0 means the carrier ETA is later than the contracted "
             "promise date.")


def project_material_runout(plant_code: str, material_code: str) -> dict:
    """When a material runs out, from scheduled consumption and stock on hand.

    Walks the forward schedule, explodes each run through the BOM, and returns
    a running balance. The first row with a negative balance is the runout.
    Returning the whole walk rather than just a date lets the agent say which
    run is the one that breaks, which is the actionable part.
    """
    return query(f"""
        WITH stock AS (
            SELECT COALESCE(SUM(i.qty_on_hand), 0) AS qty
            FROM scm.inventory_lots i
            JOIN scm.materials m ON m.material_id = i.material_id
            JOIN ops.plants p    ON p.plant_id = i.plant_id
            WHERE p.plant_code = %s AND m.material_code = %s
              AND i.status = 'available'
        ),
        sched AS (
            SELECT r.run_id, r.planned_start, l.line_code, k.sku_code,
                   k.name AS sku_name, r.planned_units,
                   r.planned_units * b.qty_per_unit AS material_needed
            FROM ops.production_runs r
            JOIN ops.lines l    ON l.line_id = r.line_id
            JOIN ops.plants p   ON p.plant_id = l.plant_id
            JOIN ops.skus k     ON k.sku_id = r.sku_id
            JOIN scm.sku_bom b  ON b.sku_id = r.sku_id
            JOIN scm.materials m ON m.material_id = b.material_id
            WHERE p.plant_code = %s AND m.material_code = %s
              AND r.planned_start >= {NOW} AND r.status = 'scheduled'
        )
        SELECT sched.planned_start, sched.line_code, sched.sku_code,
               sched.sku_name, sched.planned_units,
               round(sched.material_needed, 1) AS material_needed,
               round((SELECT qty FROM stock)
                     - SUM(sched.material_needed) OVER (ORDER BY sched.planned_start,
                                                                sched.run_id), 1)
                   AS balance_after_run
        FROM sched
        ORDER BY sched.planned_start, sched.run_id
        LIMIT 40
    """, (plant_code, material_code, plant_code, material_code),
        note="The first row where balance_after_run goes negative is the run "
             "that cannot be built. Compare that timestamp to the PO ETA.")
