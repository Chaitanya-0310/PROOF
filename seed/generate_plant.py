"""
Generate a synthetic multi-plant bakery network into Postgres.

Why synthetic: a hiring manager can clone this and have a running plant in
90 seconds with no data-sharing conversation. The SHAPE is what matters --
swap the seeder for MES/ERP replication and nothing downstream changes.

Determinism is a hard requirement, for two reasons:
  1. The demos must replay identically on any machine on any day.
  2. Phase 5 evals assert on specific numbers; a moving dataset makes
     regression testing meaningless.
So: fixed RNG seed, explicit primary keys, and a simulation clock anchored
to a fixed timestamp rather than wall-clock now().

Realism notes (the parts I would defend in an interview):
  * Downtime duration is lognormal, not uniform. Most stoppages are short;
    the tail is what hurts. A uniform distribution would make the whole
    dataset feel invented.
  * Changeovers are frequent and short; mechanical failures are rare and
    long. Those are different populations and are generated separately.
  * Scrap has a floor (startup loss on every run) plus event-driven spikes.
  * Sweet goods proof faster than artisan breads, so they are far more
    exposed to a stoppage. That asymmetry is the point of Demo 1.

Run:  make seed
"""
from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.db import connect  # noqa: E402

SEED = 42
rng = random.Random(SEED)

# The anchor. Tuesday 2026-03-17, 06:40 EDT -- mid-way through the night
# shift's final hours, which is exactly when a stoppage hurts most because
# the morning QSR trucks are already scheduled.
NOW = datetime(2026, 3, 17, 10, 40, tzinfo=timezone.utc)  # 06:40 EDT

HISTORY_DAYS = 90
FORWARD_DAYS = 3

# ---------------------------------------------------------------------
# Master data definitions
# ---------------------------------------------------------------------

PLANTS = [
    # (plant_id, code, name, city, region)
    (1, "TOR1", "Toronto Bakery 1", "Toronto", "CA-ON"),
    (2, "BRM1", "Brampton Bakery 1", "Brampton", "CA-ON"),
    (3, "DAL1", "Dallas Bakery 1", "Dallas", "US-TX"),
]

# (line_id, plant_id, code, name, type, nameplate units/hr)
LINES = [
    (1, 1, "L1", "Flatbread Line 1", "flatbread", 9000),
    (2, 1, "L2", "Flatbread Line 2", "flatbread", 9000),
    (3, 1, "L3", "Flatbread Line 3", "flatbread", 7500),
    (4, 1, "L4", "Artisan Line 4", "artisan", 4200),
    (5, 1, "L5", "Flatbread Line 5", "flatbread", 6800),
    (6, 2, "L1", "Artisan Line 1", "artisan", 4800),
    (7, 2, "L2", "Artisan Line 2", "artisan", 4800),
    (8, 2, "L3", "Sweet Goods Line 3", "sweet_goods", 12000),
    (9, 2, "L4", "Sweet Goods Line 4", "sweet_goods", 11000),
    (10, 3, "L1", "Sweet Goods Line 1", "sweet_goods", 14000),
    (11, 3, "L2", "Sweet Goods Line 2", "sweet_goods", 13000),
    (12, 3, "L3", "Flatbread Line 3", "flatbread", 8000),
]

# SKU templates per category: (name, allergens, shelf_life_days, units_per_case)
SKU_TEMPLATES = {
    "flatbread": [
        ("Original Naan", ["wheat", "milk"], 21, 96),
        ("Garlic Naan", ["wheat", "milk"], 21, 96),
        ("Tandoori Roti", ["wheat"], 21, 120),
        ("Mini Naan", ["wheat", "milk"], 21, 192),
        ("Whole Grain Naan", ["wheat", "milk"], 18, 96),
        ("Pita Pocket 6in", ["wheat", "sesame"], 14, 144),
        ("Naan Dippers", ["wheat", "milk"], 21, 160),
        ("Flatbread Pizza Crust", ["wheat"], 30, 72),
    ],
    "artisan": [
        ("Par-Baked Baguette", ["wheat"], 5, 48),
        ("Sourdough Boule", ["wheat"], 5, 36),
        ("Ciabatta Roll", ["wheat"], 5, 120),
        ("Multigrain Loaf", ["wheat", "soy", "sesame"], 7, 48),
        ("Brioche Bun", ["wheat", "milk", "egg"], 7, 96),
        ("Pretzel Bun", ["wheat", "milk"], 7, 96),
        ("Focaccia Slab", ["wheat"], 5, 24),
        ("Rustic Dinner Roll", ["wheat", "milk"], 7, 240),
    ],
    "sweet_goods": [
        ("Glazed Ring Donut", ["wheat", "milk", "egg", "soy"], 3, 144),
        ("Chocolate Dip Donut", ["wheat", "milk", "egg", "soy"], 3, 144),
        ("Boston Cream Donut", ["wheat", "milk", "egg", "soy"], 3, 96),
        ("Honey Cruller", ["wheat", "milk", "egg"], 3, 120),
        ("Butter Croissant", ["wheat", "milk", "egg"], 5, 72),
        ("Chocolate Croissant", ["wheat", "milk", "egg", "soy"], 5, 72),
        ("Cinnamon Roll", ["wheat", "milk", "egg"], 5, 60),
        ("Apple Fritter", ["wheat", "milk", "egg"], 3, 96),
    ],
}

# Proof window by category, in minutes. Sweet goods are the most fragile.
PROOF_WINDOW = {"flatbread": 95, "artisan": 150, "sweet_goods": 70}

CUSTOMERS = [
    # (id, code, name, channel, priority_tier)
    (1, "QSRA", "Northline Coffee Co", "qsr", "strategic"),
    (2, "QSRB", "Maple Drive-Thru Group", "qsr", "strategic"),
    (3, "GROC1", "Dominion Grocers DC-East", "grocery", "core"),
    (4, "GROC2", "PrairieMart DC-Central", "grocery", "core"),
    (5, "GROC3", "Lone Star Foods DC", "grocery", "core"),
    (6, "FS1", "Atlas Foodservice", "foodservice", "standard"),
    (7, "FS2", "Harbour Catering Supply", "foodservice", "standard"),
]

SUPPLIERS = [
    (1, "SUP-MILL", "Great Lakes Milling", 7),
    (2, "SUP-DAIRY", "Clearbrook Dairy", 3),
    (3, "SUP-FAT", "Prairie Oils & Fats", 10),
    (4, "SUP-YEAST", "Fermenta Ingredients", 5),
    (5, "SUP-PACK", "Northgate Packaging", 14),
]

MATERIALS = [
    # (id, code, name, uom, allergens)
    (1, "FLR-HRS", "Hard Red Spring Flour", "kg", ["wheat"]),
    (2, "FLR-WW", "Whole Wheat Flour", "kg", ["wheat"]),
    (3, "YST-INST", "Instant Dry Yeast", "kg", []),
    (4, "BTR-82", "Butter 82% Unsalted", "kg", ["milk"]),
    (5, "OIL-CAN", "Canola Oil", "L", []),
    (6, "SGR-GRN", "Granulated Sugar", "kg", []),
    (7, "EGG-LIQ", "Liquid Whole Egg", "kg", ["egg"]),
    (8, "MLK-PWD", "Skim Milk Powder", "kg", ["milk"]),
    (9, "SLT-FINE", "Fine Sea Salt", "kg", []),
    (10, "PKG-FILM", "Printed Barrier Film", "ea", []),
]

# Downtime populations. Each is (weight, reason_code, mu, sigma, eta_bias)
# where duration ~ lognormal(mu, sigma) in minutes. Two distinct populations:
# frequent-and-short (changeover, staffing) vs rare-and-long (mechanical).
DOWNTIME_KINDS = [
    (0.34, "CHANGEOVER", math.log(28), 0.35),
    (0.20, "STAFFING", math.log(12), 0.55),
    (0.14, "MATERIAL_OUT", math.log(22), 0.70),
    (0.12, "MECHANICAL", math.log(52), 0.85),
    (0.08, "ELECTRICAL", math.log(31), 0.75),
    (0.06, "QUALITY_HOLD", math.log(38), 0.60),
    (0.04, "SANITATION", math.log(65), 0.30),
    (0.02, "PLANNED_MAINT", math.log(120), 0.35),
]

# Free-text operator notes per reason code. Deliberately inconsistent in
# register and spelling -- this is the training signal for the Phase 7
# reason-code classifier, and real MES free-text looks exactly like this.
OPERATOR_NOTES = {
    "CHANGEOVER": [
        "c/o to next sku", "changeover + allergen wash", "sku change, full sanitation",
        "changeover ran long, tooling swap",
    ],
    "STAFFING": [
        "short 2 on packaging", "waiting on relief operator", "break coverage gap",
        "no packer avail",
    ],
    "MATERIAL_OUT": [
        "out of film", "waiting flour tote from WH", "no butter at line",
        "ingredient not staged",
    ],
    "MECHANICAL": [
        "sheeter gearbox noise, stopped", "conveyor belt tracking off",
        "divider jam", "oven chain fault", "depositor nozzle blocked",
    ],
    "ELECTRICAL": [
        "VFD fault on main drive", "panel e-stop reset", "sensor fault proofer",
    ],
    "QUALITY_HOLD": [
        "metal detector reject spike", "QA hold pending check weight",
        "colour out of spec",
    ],
    "SANITATION": ["allergen wash down", "mid-shift sanitation", "CIP cycle"],
    "PLANNED_MAINT": ["PM window", "scheduled maintenance", "weekly PM"],
}

SCRAP_BY_KIND = {
    "CHANGEOVER": "CHANGEOVER_PURGE",
    "MECHANICAL": "MISSHAPE",
    "ELECTRICAL": "UNDERBAKE",
    "QUALITY_HOLD": "QUALITY_REJECT",
    "MATERIAL_OUT": "MISSHAPE",
}


def lognormal_minutes(mu: float, sigma: float, lo: int = 4, hi: int = 480) -> int:
    return max(lo, min(hi, int(rng.lognormvariate(mu, sigma))))


def build_skus():
    """40 SKUs across the three categories."""
    skus = []
    sku_id = 0
    for category, templates in SKU_TEMPLATES.items():
        for name, allergens, shelf, upc in templates:
            sku_id += 1
            skus.append((
                sku_id,
                f"{category[:3].upper()}-{sku_id:03d}",
                name,
                category,
                allergens,
                shelf,
                upc,
                True,
                PROOF_WINDOW[category] + rng.randint(-10, 10),
            ))
    # Pad to 40 with size/format variants so the catalogue feels plant-sized.
    variants = ["Retail 4pk", "Club 12pk", "Foodservice Bulk", "Value 2pk"]
    base = list(skus)
    while len(skus) < 40:
        src = base[len(skus) % len(base)]
        sku_id += 1
        skus.append((
            sku_id,
            f"{src[3][:3].upper()}-{sku_id:03d}",
            f"{src[2]} {variants[len(skus) % len(variants)]}",
            src[3], src[4], src[5], src[6], True,
            PROOF_WINDOW[src[3]] + rng.randint(-10, 10),
        ))
    return skus


def build_compat(skus):
    """Which line can run which SKU. Category must match; rate and changeover vary."""
    rows = []
    for line_id, _plant, _code, _name, line_type, nameplate in LINES:
        for s in skus:
            if s[3] != line_type:
                continue
            # Line-specific rate: 78-98% of nameplate.
            rate = int(nameplate * rng.uniform(0.78, 0.98))
            # Allergen-heavy SKUs need a longer wet wash to switch to.
            base_co = 22 + 6 * len(s[4])
            rows.append((line_id, s[0], rate, base_co + rng.randint(-4, 10)))
    return rows


def main() -> None:
    print(f"Seeding PROOF (seed={SEED}, sim now={NOW.isoformat()})")
    skus = build_skus()
    compat = build_compat(skus)
    compat_by_line = {}
    for line_id, sku_id, rate, co in compat:
        compat_by_line.setdefault(line_id, []).append((sku_id, rate, co))

    sku_by_id = {s[0]: s for s in skus}
    line_by_id = {l[0]: l for l in LINES}

    runs, downtime, wip, scrap = [], [], [], []
    run_id = downtime_id = wip_id = scrap_id = 0

    start_day = NOW - timedelta(days=HISTORY_DAYS)
    end_day = NOW + timedelta(days=FORWARD_DAYS)

    for line_id, plant_id, _code, _name, line_type, _nameplate in LINES:
        options = compat_by_line[line_id]
        cursor = start_day
        last_sku = None
        while cursor < end_day:
            sku_id, rate, changeover = rng.choice(options)
            # A run is one shift block: 5-9 hours.
            hours = rng.uniform(5, 9)
            planned_start = cursor
            planned_end = planned_start + timedelta(hours=hours)
            planned_units = int(rate * hours)

            run_id += 1
            if planned_end < NOW:
                status = "complete"
            elif planned_start <= NOW < planned_end:
                status = "running"
            else:
                status = "scheduled"

            # --- downtime inside this run -------------------------------
            run_downtime_min = 0
            n_events = rng.choices([0, 1, 2, 3, 4], weights=[18, 34, 28, 14, 6])[0]
            # Planted signal for Demo 3: DAL1 sweet-goods line 10 has been
            # changing over far more often in the last 30 days (a scheduling
            # change nobody flagged). This is the discoverable root cause.
            if line_id == 10 and planned_start > NOW - timedelta(days=30):
                n_events += rng.choices([1, 2, 3], weights=[40, 40, 20])[0]

            for _ in range(n_events):
                weights = [k[0] for k in DOWNTIME_KINDS]
                if line_id == 10 and planned_start > NOW - timedelta(days=30):
                    # Skew the planted events toward changeovers specifically.
                    weights = [w * 3 if k[1] == "CHANGEOVER" else w
                               for w, k in zip(weights, DOWNTIME_KINDS)]
                kind = rng.choices(DOWNTIME_KINDS, weights=weights)[0]
                _w, reason, mu, sigma = kind
                dur = lognormal_minutes(mu, sigma)
                offset = rng.uniform(0.05, 0.9) * hours
                dstart = planned_start + timedelta(hours=offset)
                dend = dstart + timedelta(minutes=dur)
                if dstart >= NOW:
                    continue  # don't invent downtime in the future
                downtime_id += 1
                downtime.append((
                    downtime_id, line_id, run_id, reason,
                    rng.choice(OPERATOR_NOTES[reason]),
                    dstart, min(dend, NOW), dur,
                ))
                run_downtime_min += dur

                # Event-driven scrap.
                if reason in SCRAP_BY_KIND and rng.random() < 0.55:
                    scrap_id += 1
                    lost = int(rate / 60 * dur * rng.uniform(0.02, 0.18))
                    if lost > 0:
                        scrap.append((scrap_id, run_id, line_id, sku_id, lost,
                                      SCRAP_BY_KIND[reason], dstart, "line"))

            # --- actuals ------------------------------------------------
            if status == "complete":
                lost_units = int(rate / 60 * run_downtime_min)
                actual_units = max(0, planned_units - lost_units
                                   - int(planned_units * rng.uniform(0.0, 0.01)))
                actual_start = planned_start + timedelta(minutes=rng.randint(0, 12))
                actual_end = planned_end + timedelta(minutes=run_downtime_min)
            elif status == "running":
                actual_units = int(planned_units * rng.uniform(0.2, 0.7))
                actual_start = planned_start + timedelta(minutes=rng.randint(0, 12))
                actual_end = None
            else:
                actual_units, actual_start, actual_end = 0, None, None

            runs.append((run_id, line_id, sku_id, planned_start, planned_end,
                         actual_start, actual_end, planned_units, actual_units, status))

            # Every run carries a startup-loss floor.
            scrap_id += 1
            scrap.append((scrap_id, run_id, line_id, sku_id,
                          int(planned_units * rng.uniform(0.004, 0.016)),
                          "STARTUP_LOSS",
                          planned_start + timedelta(minutes=8), "line"))

            # --- WIP staged for runs near or after 'now' ----------------
            if planned_end > NOW - timedelta(hours=6):
                window = sku_by_id[sku_id][8]
                for i in range(rng.randint(2, 4)):
                    wip_id += 1
                    staged = planned_start + timedelta(hours=rng.uniform(0, hours))
                    wip.append((
                        wip_id, f"WIP-{wip_id:06d}", run_id, line_id, sku_id,
                        int(rate * rng.uniform(0.15, 0.4)),
                        staged, staged + timedelta(minutes=window),
                        "consumed" if staged + timedelta(minutes=window) < NOW else "staged",
                    ))

            last_sku = sku_id
            cursor = planned_end + timedelta(minutes=changeover if last_sku != sku_id else 10)

    print(f"  generated {len(runs):,} runs, {len(downtime):,} downtime events, "
          f"{len(wip):,} wip batches, {len(scrap):,} scrap events")

    # ---------------- customer orders + allocations ----------------
    orders, order_lines, allocations = [], [], []
    order_id = order_line_id = 0
    # Allocate every run from the last 10 days forward to a customer order,
    # so that "which order does this run serve?" always has an answer.
    for r in runs:
        rid, line_id, sku_id, pstart, pend, *_rest = r
        if pend < NOW - timedelta(days=10):
            continue
        plant_id = line_by_id[line_id][1]
        cust = rng.choice(CUSTOMERS)
        order_id += 1
        order_line_id += 1
        # QSR ships same morning; grocery gets a next-day window.
        ship_lag = timedelta(hours=2) if cust[3] == "qsr" else timedelta(hours=20)
        orders.append((order_id, f"SO-{100000 + order_id}", cust[0], plant_id,
                       pend + ship_lag, "open" if pend > NOW else "shipped"))
        qty = int(r[7] * rng.uniform(0.55, 0.95))
        order_lines.append((order_line_id, order_id, sku_id, qty,
                            qty if pend < NOW else 0))
        allocations.append((rid, order_line_id, qty))

    # ---------------- inventory + purchase orders ----------------
    lots, pos = [], []
    lot_id = po_id = 0
    for plant_id, *_ in [(p[0],) for p in PLANTS]:
        for m in MATERIALS:
            for _ in range(rng.randint(2, 4)):
                lot_id += 1
                received = NOW - timedelta(days=rng.randint(1, 45))
                lots.append((
                    lot_id, f"LOT-{lot_id:06d}", plant_id, m[0],
                    round(rng.uniform(1200, 26000), 3), m[3], received,
                    received + timedelta(days=rng.randint(60, 400)),
                    "available" if rng.random() > 0.06 else "held",
                ))
            # One or two open POs per material per plant.
            for _ in range(rng.randint(1, 2)):
                po_id += 1
                sup = rng.choice(SUPPLIERS)
                ordered = NOW - timedelta(days=rng.randint(2, 20))
                promised = ordered + timedelta(days=sup[3])
                pos.append((
                    po_id, f"PO-{200000 + po_id}", sup[0], plant_id, m[0],
                    round(rng.uniform(8000, 40000), 3), m[3],
                    ordered, promised, promised,  # eta == promised; Demo 2 slips one
                    "in_transit" if promised > NOW else "received",
                ))

    write_all(skus, compat, runs, downtime, wip, scrap,
              orders, order_lines, allocations, lots, pos)


def write_all(skus, compat, runs, downtime, wip, scrap,
              orders, order_lines, allocations, lots, pos) -> None:
    with connect("seeder") as conn:
        with conn.cursor() as cur:
            # No RESTART IDENTITY: that needs sequence OWNERSHIP, which the
            # seeder deliberately does not have. Sequences are fast-forwarded
            # with setval() at the end of this function instead.
            print("  truncating...")
            cur.execute("""
                TRUNCATE scm.run_order_allocations, scm.customer_order_lines,
                         scm.customer_orders, scm.purchase_orders,
                         scm.inventory_lots, qms.quality_holds,
                         ops.scrap_events, ops.wip_batches, ops.downtime_events,
                         ops.production_runs, ops.line_sku_compat, scm.sku_bom,
                         ops.skus, ops.lines, ops.plants, scm.materials,
                         scm.suppliers, scm.customers, ops.sim_clock
                CASCADE
            """)

            cur.execute("INSERT INTO ops.sim_clock (id, now_ts) VALUES (1, %s)", (NOW,))
            cur.executemany(
                "INSERT INTO ops.plants VALUES (%s,%s,%s,%s,%s)", PLANTS)
            cur.executemany(
                "INSERT INTO ops.lines VALUES (%s,%s,%s,%s,%s,%s)", LINES)
            cur.executemany(
                "INSERT INTO scm.customers VALUES (%s,%s,%s,%s,%s)", CUSTOMERS)
            cur.executemany(
                "INSERT INTO scm.suppliers VALUES (%s,%s,%s,%s)", SUPPLIERS)
            cur.executemany(
                "INSERT INTO scm.materials VALUES (%s,%s,%s,%s,%s)", MATERIALS)
            cur.executemany(
                "INSERT INTO ops.skus VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", skus)
            cur.executemany(
                "INSERT INTO ops.line_sku_compat VALUES (%s,%s,%s,%s)", compat)

            # Simple BOM: flour + yeast + salt for everything, plus category extras.
            bom = []
            for s in skus:
                bom += [(s[0], 1, 0.062), (s[0], 3, 0.0009), (s[0], 9, 0.0011)]
                if "milk" in s[4]:
                    bom.append((s[0], 8, 0.004))
                if "egg" in s[4]:
                    bom.append((s[0], 7, 0.006))
                if s[3] == "sweet_goods":
                    bom += [(s[0], 4, 0.009), (s[0], 6, 0.012)]
                else:
                    bom.append((s[0], 5, 0.003))
            cur.executemany(
                "INSERT INTO scm.sku_bom VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", bom)

            print("  writing operational tables...")
            cur.executemany(
                "INSERT INTO ops.production_runs "
                "(run_id,line_id,sku_id,planned_start,planned_end,actual_start,"
                "actual_end,planned_units,actual_units,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", runs)
            cur.executemany(
                "INSERT INTO ops.downtime_events "
                "(event_id,line_id,run_id,reason_code,reason_detail,started_at,"
                "ended_at,eta_minutes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", downtime)
            cur.executemany(
                "INSERT INTO ops.wip_batches "
                "(batch_id,batch_code,run_id,line_id,sku_id,units,staged_at,"
                "proof_expires_at,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", wip)
            cur.executemany(
                "INSERT INTO ops.scrap_events "
                "(scrap_id,run_id,line_id,sku_id,units,reason_code,occurred_at,source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", scrap)

            print("  writing supply chain tables...")
            cur.executemany(
                "INSERT INTO scm.inventory_lots "
                "(lot_id,lot_code,plant_id,material_id,qty_on_hand,uom,received_at,"
                "expires_at,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", lots)
            cur.executemany(
                "INSERT INTO scm.purchase_orders "
                "(po_id,po_code,supplier_id,plant_id,material_id,qty,uom,ordered_at,"
                "promised_at,eta_at,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", pos)
            cur.executemany(
                "INSERT INTO scm.customer_orders "
                "(order_id,order_code,customer_id,plant_id,promised_ship_at,status) "
                "VALUES (%s,%s,%s,%s,%s,%s)", orders)
            cur.executemany(
                "INSERT INTO scm.customer_order_lines "
                "(order_line_id,order_id,sku_id,qty_units,qty_fulfilled) "
                "VALUES (%s,%s,%s,%s,%s)", order_lines)
            cur.executemany(
                "INSERT INTO scm.run_order_allocations VALUES (%s,%s,%s)", allocations)

            # Explicit IDs were used throughout, so sequences must be fast-forwarded
            # or the first application INSERT in Phase 4 would collide.
            for tbl, col in [
                ("ops.plants", "plant_id"), ("ops.lines", "line_id"),
                ("ops.skus", "sku_id"), ("scm.customers", "customer_id"),
                ("scm.suppliers", "supplier_id"), ("scm.materials", "material_id"),
                ("ops.production_runs", "run_id"), ("ops.downtime_events", "event_id"),
                ("ops.wip_batches", "batch_id"), ("ops.scrap_events", "scrap_id"),
                ("scm.inventory_lots", "lot_id"), ("scm.purchase_orders", "po_id"),
                ("scm.customer_orders", "order_id"),
                ("scm.customer_order_lines", "order_line_id"),
            ]:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{tbl}','{col}'), "
                    f"COALESCE((SELECT MAX({col}) FROM {tbl}), 1))")
        conn.commit()
    print("  done.")


if __name__ == "__main__":
    main()
