"""Demand MCP server -- stands in for the ERP order book.

Converts a plant event into a commercial consequence. Without this domain a
stoppage is a status update; with it, it is a decision.
"""
from __future__ import annotations

from _serve import build, text_result

from proof.tools import demand

server = build(
    "proof-demand",
    "The customer order book: who is owed what, by when, and who gets hurt "
    "when production falls short. Customers carry a priority_tier -- "
    "strategic, core, standard -- which is the ranking to use when not every "
    "commitment can be met. This server knows nothing about line state; ask "
    "the production server for that.",
)


@server.tool()
def get_orders_for_run(run_id: int) -> str:
    """Which customer orders a production run is committed to.

    Uses explicit run-to-order allocations rather than inferring from SKU and
    date, so the answer is a fact rather than a guess.

    Args:
        run_id: Numeric run id, as returned by get_open_downtime.
    """
    return text_result(demand.get_orders_for_run(run_id))


@server.tool()
def get_at_risk_orders(plant_code: str, within_hours: int = 24) -> str:
    """Open orders shipping soon whose supplying line is stopped or behind.

    Ordered by priority_tier then ship time, so the first row is the one to
    protect.

    Args:
        plant_code: Plant code, e.g. TOR1.
        within_hours: Ship-window horizon.
    """
    return text_result(demand.get_at_risk_orders(plant_code, within_hours))


@server.tool()
def get_order_shortfall(order_code: str) -> str:
    """Exact shortfall on one order given what its runs have actually made.

    Args:
        order_code: Order code, e.g. SO-100123.
    """
    return text_result(demand.get_order_shortfall(order_code))


@server.tool()
def get_customer_exposure(plant_code: str, sku_code: str,
                          within_hours: int = 48) -> str:
    """All near-term commitments for one SKU, ranked by customer priority.

    Use when output is short and someone has to go without.

    Args:
        plant_code: Plant code, e.g. TOR1.
        sku_code: SKU code, e.g. FLA-002.
        within_hours: Ship-window horizon.
    """
    return text_result(
        demand.get_customer_exposure(plant_code, sku_code, within_hours))


if __name__ == "__main__":
    server.run("stdio")
