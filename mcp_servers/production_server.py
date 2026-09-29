"""Production MCP server -- stands in for MES/SCADA.

Exposes line state, downtime, output-loss arithmetic, alternate-line search
and scrap analytics. Six tools, deliberately: this is the narrow surface the
Production Agent reasons over.
"""
from __future__ import annotations

from _serve import build, text_result

from proof.tools import production

server = build(
    "proof-production",
    "Live production state for a bakery network: what each line is running, "
    "what is stopped, what a stoppage costs in output, and scrap analytics. "
    "This server knows NOTHING about perishable WIP expiry, customer orders "
    "or raw materials -- ask the inventory, demand and quality servers for "
    "those. Output loss and WIP scrap are separate numbers that must be added.",
)


@server.tool()
def get_open_downtime(plant_code: str | None = None) -> str:
    """List every production line that is stopped right now.

    Returns elapsed downtime and the operator's estimated time to restore
    (operator_eta_minutes), plus line_id and run_id for follow-up calls.
    Start here for any "what is at risk" question.

    Args:
        plant_code: Restrict to one plant, e.g. TOR1. Omit for all plants.
    """
    return text_result(production.get_open_downtime(plant_code))


@server.tool()
def get_line_status(line_id: int) -> str:
    """What a line is currently running: SKU, rate, plan vs actual, schedule.

    Args:
        line_id: Numeric line id, as returned by get_open_downtime.
    """
    return text_result(production.get_line_status(line_id))


@server.tool()
def estimate_output_loss(line_id: int, minutes_down: int) -> str:
    """Units and cases NOT produced if a line stays down this long.

    Uses the line-specific rate for the SKU actually running, not nameplate.
    This is ONLY lost throughput -- it excludes staged dough that expires
    during the stoppage, which is usually the larger loss.

    Args:
        line_id: Numeric line id.
        minutes_down: How long the line is expected to remain stopped.
    """
    return text_result(production.estimate_output_loss(line_id, minutes_down))


@server.tool()
def find_alternate_lines(plant_code: str, sku_id: int | None = None,
                         sku_code: str | None = None) -> str:
    """Lines at a plant that could run this SKU, and the changeover cost.

    A line is genuinely available only when currently_running_run is null and
    is_currently_down is false.

    Args:
        plant_code: Plant to search within, e.g. TOR1.
        sku_id: Numeric SKU id. Pass this or sku_code.
        sku_code: SKU code, e.g. FLA-002. Pass this or sku_id.
    """
    return text_result(
        production.find_alternate_lines(sku_id, plant_code, sku_code))


@server.tool()
def get_scrap_breakdown(days: int = 30, plant_code: str | None = None,
                        group_by: str = "line") -> str:
    """Scrap rate over a window, sliced by line, sku, category or reason.

    Args:
        days: Lookback window in days.
        plant_code: Restrict to one plant. Omit for all plants.
        group_by: One of line, sku, category, reason. 'reason' returns share
            of total scrap rather than a rate.
    """
    return text_result(production.get_scrap_breakdown(days, plant_code, group_by))


@server.tool()
def compare_scrap_windows(line_id: int, recent_days: int = 30,
                          baseline_days: int = 90) -> str:
    """Compare a line's recent scrap and downtime against its own baseline.

    Returns changeovers per run alongside scrap percent, so a correlation
    between the two is visible rather than assumed. Use for "why is scrap up
    on X" questions.

    Args:
        line_id: Numeric line id.
        recent_days: Size of the recent window.
        baseline_days: Total lookback; the remainder forms the baseline.
    """
    return text_result(
        production.compare_scrap_windows(line_id, recent_days, baseline_days))


if __name__ == "__main__":
    server.run("stdio")
