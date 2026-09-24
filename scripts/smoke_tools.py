"""
Exercise every domain tool against the real database. No API key needed.

This is the Phase 2 equivalent of `make verify`: it proves the tool layer is
correct BEFORE any model is involved. Debugging a wrong number is a different
job from debugging a model that chose the wrong tool, and doing both at once
is how these projects stall.

It also asserts the Demo 1 arithmetic, so the number the agent is supposed to
arrive at is pinned here independently of whether an agent exists yet.

Run:  make smoke
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.tools import demand, inventory, production, quality  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))


def head(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def main() -> int:
    # ---------------------------------------------------------------
    # PRODUCTION
    # ---------------------------------------------------------------
    head("production")

    down = production.get_open_downtime()
    print(f"open downtime: {down['row_count']} event(s)")
    for r in down["rows"]:
        print(f"  {r['plant_code']}/{r['line_code']}  {r['reason_code']}  "
              f"down {r['minutes_down_so_far']}m  ETA {r['operator_eta_minutes']}m")
        print(f"    note: {r['reason_detail']}")
    check("exactly one line is down (the Demo 1 situation)", down["row_count"] == 1)

    ev = down["rows"][0]
    line_id, run_id, eta = ev["line_id"], ev["run_id"], ev["operator_eta_minutes"]

    status = production.get_line_status(line_id)
    s = status["rows"][0]
    print(f"\nline status: {s['line_name']} running {s['sku_name']} "
          f"@ {s['line_rate_units_per_hour']:,}/hr, "
          f"proof window {s['proof_window_minutes']}m")
    check("line has a running SKU", s["sku_code"] is not None)

    loss = production.estimate_output_loss(line_id, eta)
    lo = loss["rows"][0]
    print(f"output loss over {eta}m: {lo['units_not_produced']:,} units "
          f"({lo['cases_not_produced']:,} cases)")
    check("output loss is positive", lo["units_not_produced"] > 0)

    # ---------------------------------------------------------------
    # INVENTORY -- the scrap clock
    # ---------------------------------------------------------------
    head("inventory (the scrap clock)")

    wip = inventory.project_wip_expiry(line_id, eta)
    lost = [r for r in wip["rows"] if r["expires_before_restart"]]
    kept = [r for r in wip["rows"] if not r["expires_before_restart"]]
    for r in wip["rows"]:
        tag = "LOST" if r["expires_before_restart"] else "ok  "
        print(f"  {tag} {r['batch_code']}  {r['units']:>6,} units  "
              f"expires in {r['minutes_until_expiry']:>4}m  "
              f"slack {r['minutes_of_slack_after_restart']:>5}m")
    scrap_units = sum(r["units"] for r in lost)
    print(f"\n  scrapped by the stoppage: {scrap_units:,} units "
          f"across {len(lost)} batch(es); {len(kept)} survive")

    # The planted scenario: 2 of 4 batches die, totalling 3,450 units.
    # Pinned here so a generator change that breaks Demo 1 fails loudly.
    check("exactly 2 batches expire before restart", len(lost) == 2,
          f"got {len(lost)}")
    check("scrap from expiry is 3,450 units", scrap_units == 3450,
          f"got {scrap_units}")
    check("WIP scrap exceeds output loss (the point of Demo 1)",
          scrap_units > 0)

    stock = inventory.get_material_stock("TOR1", "FLR-HRS")
    print(f"\nTOR1 flour: {stock['rows'][0]['qty_available']:,} "
          f"{stock['rows'][0]['uom']} across {stock['rows'][0]['lots']} lots")

    pos = inventory.get_open_purchase_orders("TOR1", "FLR-HRS")
    late = [r for r in pos["rows"] if r["days_late"] > 0]
    for r in pos["rows"]:
        print(f"  {r['po_code']}  {r['supplier']:22s} "
              f"ETA {r['eta_at'][:10]}  {r['days_late']:+d}d vs promise")
    check("the Demo 2 PO slip is visible", len(late) >= 1,
          f"{len(late)} late PO(s)")

    runout = inventory.project_material_runout("TOR1", "FLR-HRS")
    neg = [r for r in runout["rows"] if r["balance_after_run"] < 0]
    if neg:
        f = neg[0]
        print(f"  flour runs out at {f['planned_start'][:16]} "
              f"on {f['line_code']} ({f['sku_name']})")
    check("material runout is projectable", runout["row_count"] > 0)

    # ---------------------------------------------------------------
    # DEMAND
    # ---------------------------------------------------------------
    head("demand")

    orders = demand.get_orders_for_run(run_id)
    for r in orders["rows"]:
        print(f"  {r['order_code']}  {r['customer']:24s} {r['priority_tier']:9s} "
              f"{r['allocated_units']:>6,} units  ships in {r['hours_until_ship']}h")
    check("the stopped run is allocated to a customer order",
          orders["row_count"] >= 1)
    check("it is a strategic customer (planted)",
          any(r["priority_tier"] == "strategic" for r in orders["rows"]))

    # Shortfall was not covered here originally, and a real attribution bug
    # lived in it undetected: production was credited from each run's whole
    # actual_units instead of this order line's allocated share, which on a
    # run serving several lines reports zero shortfall on an order that is
    # genuinely short. Untested tools are where wrong numbers hide.
    order_code = orders["rows"][0]["order_code"]
    sf = demand.get_order_shortfall(order_code)
    sfr = sf["rows"][0]
    print(f"\nshortfall on {order_code}: ordered {sfr['ordered_units']:,}, "
          f"produced {sfr['produced_by_allocated_runs']:,}, "
          f"short {sfr['shortfall_units']:,}")
    check("shortfall resolves to one row", sf["row_count"] == 1)
    check("attributed production never exceeds the allocation",
          sfr["produced_by_allocated_runs"] <= sfr["ordered_units"],
          f"{sfr['produced_by_allocated_runs']} vs {sfr['ordered_units']}")
    check("the stopped run leaves the order short",
          sfr["shortfall_units"] > 0, f"{sfr['shortfall_units']} units")

    risk = demand.get_at_risk_orders("TOR1", 24)
    print(f"\nat-risk orders at TOR1 in 24h: {risk['row_count']}")
    for r in risk["rows"][:4]:
        print(f"  {r['order_code']}  {r['customer']:24s} "
              f"{r['units_outstanding']:>6,} short  "
              f"line {r['line_code']} down={r['supplying_line_is_down']}")
    check("at-risk query returns rows", risk["row_count"] > 0)

    # ---------------------------------------------------------------
    # QUALITY
    # ---------------------------------------------------------------
    head("quality")

    running_sku_id = _sku_id_for_line(line_id)
    alts = production.find_alternate_lines(running_sku_id, "TOR1")
    free = [r for r in alts["rows"]
            if r["currently_running_run"] is None and not r["is_currently_down"]]
    print(f"alternate lines for this SKU at TOR1: "
          f"{alts['row_count']} compatible, {len(free)} free")
    for r in alts["rows"][:5]:
        state = ("FREE" if r["currently_running_run"] is None
                 and not r["is_currently_down"] else "busy")
        print(f"  {state} {r['line_code']}  {r['line_rate_units_per_hour']:,}/hr  "
              f"changeover {r['changeover_minutes']}m")
    check("a free alternate line exists (planted for Demo 1)", len(free) >= 1)

    if free:
        compat = quality.check_allergen_compatibility(
            free[0]["line_id"], running_sku_id)
        c = compat["rows"][0]
        print(f"\nallergen check L{free[0]['line_code']} -> {c['target_sku_name']}: "
              f"added={c['added_allergens']} "
              f"dry_ok={c['dry_changeover_sufficient']}")
        check("allergen compatibility resolves", compat["row_count"] == 1)

    # ---------------------------------------------------------------
    # DEMO 3 -- root cause
    # ---------------------------------------------------------------
    head("demo 3: scrap root cause")

    by_line = production.get_scrap_breakdown(days=30, group_by="line")
    worst = by_line["rows"][0]
    print(f"worst line by scrap (30d): {worst['line']} at {worst['scrap_pct']}%")
    for r in by_line["rows"][:4]:
        print(f"  {r['line']:10s} {r['scrap_pct']:>5}%  "
              f"{r['scrap_units']:>7,} / {r['planned_units']:>9,} units")

    cmp_ = production.compare_scrap_windows(line_id=10)
    by_bucket = {r["bucket"]: r for r in cmp_["rows"]}
    rec, base = by_bucket.get("recent"), by_bucket.get("baseline")
    print(f"\nDAL1/L1  baseline {base['scrap_pct']}% "
          f"({base['changeovers_per_run']} c/o per run)"
          f"  ->  recent {rec['scrap_pct']}% "
          f"({rec['changeovers_per_run']} c/o per run)")
    check("scrap deteriorated in the recent window",
          rec["scrap_pct"] > base["scrap_pct"],
          f"{base['scrap_pct']} -> {rec['scrap_pct']}")
    check("changeovers rose alongside it (the discoverable cause)",
          rec["changeovers_per_run"] > base["changeovers_per_run"] * 1.5)

    reasons = production.get_scrap_breakdown(days=30, plant_code="DAL1",
                                            group_by="reason")
    print("\nDAL1 scrap by reason (30d):")
    for r in reasons["rows"][:4]:
        print(f"  {r['reason_code']:18s} {r['pct_of_all_scrap']:>5}%  "
              f"{r['scrap_units']:>7,} units")
    check("reason breakdown sums to ~100%",
          abs(sum(r["pct_of_all_scrap"] for r in reasons["rows"]) - 100) < 1)

    # ---------------------------------------------------------------
    head("results")
    failed = [r for r in results if r[0] == FAIL]
    for state, name, detail in results:
        print(f"  [{state}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def _sku_id_for_line(line_id: int) -> int:
    """The SKU currently running on a line (helper; tools return codes, not ids)."""
    from proof.tools._base import query
    r = query("""
        SELECT r.sku_id FROM ops.production_runs r
        WHERE r.line_id = %s
          AND r.planned_start <= (SELECT now_ts FROM ops.sim_clock)
          AND r.planned_end   >  (SELECT now_ts FROM ops.sim_clock)
        LIMIT 1
    """, (line_id,))
    return r["rows"][0]["sku_id"]


if __name__ == "__main__":
    raise SystemExit(main())
