"""Demand domain tools -- the order book and who gets hurt.

Stands in for the ERP order book. This is the domain that converts a plant
event into a commercial consequence, which is the only form in which a
stoppage becomes a decision rather than a status update.
"""
from __future__ import annotations

from ._base import query, sim_now_sql

NOW = sim_now_sql()


def get_orders_for_run(run_id: int) -> dict:
    """Which customer orders a production run is committed to.

    Goes through scm.run_order_allocations rather than inferring from SKU and
    date. Inference there would be guessing, and guessing about which customer
    is short is the kind of error that ends a pilot.
    """
    return query(f"""
        SELECT o.order_code, c.name AS customer, c.channel, c.priority_tier,
               k.sku_code, k.name AS sku_name,
               ol.qty_units AS ordered_units, ol.qty_fulfilled,
               a.allocated_units, o.promised_ship_at, o.status,
               round(EXTRACT(EPOCH FROM (o.promised_ship_at - {NOW})) / 3600, 1)
                   AS hours_until_ship
        FROM scm.run_order_allocations a
        JOIN scm.customer_order_lines ol ON ol.order_line_id = a.order_line_id
        JOIN scm.customer_orders o       ON o.order_id = ol.order_id
        JOIN scm.customers c             ON c.customer_id = o.customer_id
        JOIN ops.skus k                  ON k.sku_id = ol.sku_id
        WHERE a.run_id = %s
        ORDER BY o.promised_ship_at
    """, (run_id,),
        note="priority_tier ranks who to protect when you cannot satisfy "
             "everyone: strategic > core > standard.")


def get_orders_for_runs(run_ids: list[int]) -> dict:
    """Customer orders committed to ANY of a set of runs, one row per order.

    Built for a material shortage, which breaks dozens of runs at once --
    pass the short run_ids from the inventory runout. Like get_orders_for_run
    it goes through allocations, so an order here is exposed to those runs by
    fact, not by a SKU-and-date guess.
    """
    result = query(f"""
        SELECT o.order_code, c.name AS customer, c.priority_tier,
               k.sku_code, k.name AS sku_name,
               (ol.qty_units - ol.qty_fulfilled)::int AS units_outstanding,
               SUM(a.allocated_units)::int            AS units_from_these_runs,
               array_agg(DISTINCT a.run_id ORDER BY a.run_id) AS run_ids,
               min(r.planned_start)                   AS earliest_run_start,
               o.promised_ship_at,
               round(EXTRACT(EPOCH FROM (o.promised_ship_at - {NOW})) / 3600, 1)
                   AS hours_until_ship
        FROM scm.run_order_allocations a
        JOIN ops.production_runs r       ON r.run_id = a.run_id
        JOIN scm.customer_order_lines ol ON ol.order_line_id = a.order_line_id
        JOIN scm.customer_orders o       ON o.order_id = ol.order_id
        JOIN scm.customers c             ON c.customer_id = o.customer_id
        JOIN ops.skus k                  ON k.sku_id = ol.sku_id
        WHERE a.run_id = ANY(%s) AND o.status = 'open'
        GROUP BY o.order_code, c.name, c.priority_tier, k.sku_code, k.name,
                 ol.qty_units, ol.qty_fulfilled, o.promised_ship_at
        ORDER BY CASE c.priority_tier WHEN 'strategic' THEN 0
                                      WHEN 'core' THEN 1 ELSE 2 END,
                 o.promised_ship_at
    """, (list(run_ids),),
        note="units_from_these_runs is the part of each order those runs were "
             "going to make -- the units lost if none of them is built. "
             "priority_tier ranks who to protect: strategic > core > standard. "
             "Quote totals from `summary`, not by counting rows.")

    rows = result["rows"]
    tiers: dict[str, dict] = {}
    for r in rows:
        t = tiers.setdefault(r["priority_tier"], {"orders": 0, "units_from_these_runs": 0})
        t["orders"] += 1
        t["units_from_these_runs"] += r["units_from_these_runs"]
    result["summary"] = {
        "orders": len(rows),
        "customers": len({r["customer"] for r in rows}),
        "units_outstanding": sum(r["units_outstanding"] for r in rows),
        "units_from_these_runs": sum(r["units_from_these_runs"] for r in rows),
        "by_priority_tier": tiers,
        "earliest_ship": min((r["promised_ship_at"] for r in rows), default=None),
    }
    return result


def get_at_risk_orders(plant_code: str, within_hours: int = 24) -> dict:
    """Open orders shipping soon whose supplying runs are behind or stopped.

    'At risk' is defined concretely: the order still has unfulfilled units and
    at least one of its supplying runs is stopped or is running short of plan.
    A vaguer definition would flag everything and be ignored.
    """
    return query(f"""
        SELECT o.order_code, c.name AS customer, c.priority_tier,
               k.sku_code, k.name AS sku_name,
               ol.qty_units AS ordered_units, ol.qty_fulfilled,
               (ol.qty_units - ol.qty_fulfilled) AS units_outstanding,
               o.promised_ship_at,
               round(EXTRACT(EPOCH FROM (o.promised_ship_at - {NOW})) / 3600, 1)
                   AS hours_until_ship,
               l.line_code, r.run_id, r.status AS run_status,
               r.planned_units, r.actual_units,
               EXISTS (SELECT 1 FROM ops.downtime_events d
                       WHERE d.line_id = r.line_id AND d.ended_at IS NULL)
                   AS supplying_line_is_down
        FROM scm.customer_orders o
        JOIN scm.customers c             ON c.customer_id = o.customer_id
        JOIN scm.customer_order_lines ol ON ol.order_id = o.order_id
        JOIN ops.skus k                  ON k.sku_id = ol.sku_id
        LEFT JOIN scm.run_order_allocations a ON a.order_line_id = ol.order_line_id
        LEFT JOIN ops.production_runs r  ON r.run_id = a.run_id
        LEFT JOIN ops.lines l            ON l.line_id = r.line_id
        JOIN ops.plants p                ON p.plant_id = o.plant_id
        WHERE p.plant_code = %s
          AND o.status = 'open'
          AND o.promised_ship_at BETWEEN {NOW}
                                     AND {NOW} + make_interval(hours => %s)
          AND ol.qty_fulfilled < ol.qty_units
        ORDER BY
            CASE c.priority_tier WHEN 'strategic' THEN 0
                                 WHEN 'core' THEN 1 ELSE 2 END,
            o.promised_ship_at
    """, (plant_code, within_hours))


def get_order_shortfall(order_code: str) -> dict:
    """Exact shortfall on one order, given what its runs have actually made.

    Production is attributed by ALLOCATED SHARE, not by each run's whole
    output. A run can serve several order lines; crediting its full
    actual_units to every one of them counts the same cases twice and can
    report zero shortfall on an order that is genuinely short -- the most
    dangerous possible direction for this particular number to be wrong in.
    """
    return query(f"""
        SELECT o.order_code, c.name AS customer, c.priority_tier,
               k.sku_code, k.name AS sku_name,
               ol.qty_units AS ordered_units,
               ol.qty_fulfilled,
               -- each run's completion ratio, applied to this line's share
               COALESCE(SUM(a.allocated_units
                            * LEAST(r.actual_units::numeric
                                    / NULLIF(r.planned_units, 0), 1)), 0)::int
                   AS produced_by_allocated_runs,
               GREATEST(ol.qty_units - ol.qty_fulfilled
                        - COALESCE(SUM(a.allocated_units
                                       * LEAST(r.actual_units::numeric
                                               / NULLIF(r.planned_units, 0), 1)),
                                   0), 0)::int AS shortfall_units,
               o.promised_ship_at,
               round(EXTRACT(EPOCH FROM (o.promised_ship_at - {NOW})) / 3600, 1)
                   AS hours_until_ship
        FROM scm.customer_orders o
        JOIN scm.customers c             ON c.customer_id = o.customer_id
        JOIN scm.customer_order_lines ol ON ol.order_id = o.order_id
        JOIN ops.skus k                  ON k.sku_id = ol.sku_id
        LEFT JOIN scm.run_order_allocations a ON a.order_line_id = ol.order_line_id
        LEFT JOIN ops.production_runs r  ON r.run_id = a.run_id
        WHERE o.order_code = %s
        GROUP BY o.order_code, c.name, c.priority_tier, k.sku_code, k.name,
                 ol.qty_units, ol.qty_fulfilled, o.promised_ship_at
    """, (order_code,))


def get_customer_exposure(plant_code: str, sku_code: str,
                          within_hours: int = 48) -> dict:
    """All near-term commitments for one SKU, ranked by who matters most.

    Used when output is short and someone has to decide who goes without.
    """
    return query(f"""
        SELECT c.name AS customer, c.channel, c.priority_tier,
               count(DISTINCT o.order_id) AS orders,
               SUM(ol.qty_units - ol.qty_fulfilled)::int AS units_outstanding,
               min(o.promised_ship_at) AS earliest_ship
        FROM scm.customer_orders o
        JOIN scm.customers c             ON c.customer_id = o.customer_id
        JOIN scm.customer_order_lines ol ON ol.order_id = o.order_id
        JOIN ops.skus k                  ON k.sku_id = ol.sku_id
        JOIN ops.plants p                ON p.plant_id = o.plant_id
        WHERE p.plant_code = %s AND k.sku_code = %s
          AND o.status = 'open'
          AND o.promised_ship_at <= {NOW} + make_interval(hours => %s)
        GROUP BY c.name, c.channel, c.priority_tier
        ORDER BY CASE c.priority_tier WHEN 'strategic' THEN 0
                                      WHEN 'core' THEN 1 ELSE 2 END,
                 units_outstanding DESC
    """, (plant_code, sku_code, within_hours))
