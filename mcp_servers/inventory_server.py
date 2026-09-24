"""Inventory MCP server -- stands in for WMS plus the MES WIP tracker.

Owns THE SCRAP CLOCK (project_wip_expiry), which is the tool that turns a
stoppage from a throughput problem into a perishability problem.
"""
from __future__ import annotations

from _serve import build, text_result

from proof.tools import inventory

server = build(
    "proof-inventory",
    "Perishable work-in-progress on the plant floor and raw material in the "
    "warehouse. CRITICAL: proofed dough expires. When a line stops, staged "
    "dough keeps proofing and becomes scrap if the line cannot restart in "
    "time. That scrap is ADDITIONAL to lost throughput, and on sweet goods "
    "(70 minute proof window) it is usually the bigger number. Always check "
    "WIP expiry when a line is down.",
)


@server.tool()
def get_staged_wip(line_id: int) -> str:
    """Proofed dough staged on a line right now, soonest to expire first.

    Args:
        line_id: Numeric line id.
    """
    return text_result(inventory.get_staged_wip(line_id))


@server.tool()
def project_wip_expiry(line_id: int, restart_in_minutes: int) -> str:
    """THE SCRAP CLOCK. Which staged batches die before the line restarts.

    Given the restart estimate, flags each batch with expires_before_restart.
    Sum units where that is true to get units scrapped BY the stoppage. This
    is separate from, and additional to, output not produced.

    Args:
        line_id: Numeric line id.
        restart_in_minutes: Expected minutes until the line runs again --
            normally the operator_eta_minutes from the downtime event.
    """
    return text_result(inventory.project_wip_expiry(line_id, restart_in_minutes))


@server.tool()
def get_material_stock(plant_code: str, material_code: str | None = None) -> str:
    """Raw material on hand at a plant.

    Args:
        plant_code: Plant code, e.g. TOR1.
        material_code: Restrict to one material, e.g. FLR-HRS. Omit for all.
    """
    return text_result(inventory.get_material_stock(plant_code, material_code))


@server.tool()
def get_open_purchase_orders(plant_code: str,
                             material_code: str | None = None) -> str:
    """Inbound POs with days_late: carrier ETA versus the contracted date.

    Args:
        plant_code: Plant code, e.g. TOR1.
        material_code: Restrict to one material. Omit for all.
    """
    return text_result(
        inventory.get_open_purchase_orders(plant_code, material_code))


@server.tool()
def project_material_runout(plant_code: str, material_code: str) -> str:
    """Walk the forward schedule to find when a material runs out.

    Returns a running balance per scheduled run. The first row where
    balance_after_run goes negative is the run that cannot be built.

    Args:
        plant_code: Plant code, e.g. TOR1.
        material_code: Material code, e.g. FLR-HRS.
    """
    return text_result(
        inventory.project_material_runout(plant_code, material_code))


if __name__ == "__main__":
    server.run("stdio")
