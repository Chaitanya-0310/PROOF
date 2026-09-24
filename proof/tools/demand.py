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
