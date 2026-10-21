"""Business descriptions for every queryable table -- the curated half of the index.

Column names and types are introspected from Postgres; what they MEAN is not
in the catalogue. `ended_at IS NULL` meaning "the line is down right now", or
that a shortfall must go through run_order_allocations, is exactly the kind
of fact a model gets wrong from DDL alone and a plant engineer would correct
in a sentence. This file is where those sentences live.

Each entry:
  description  one or two sentences, shown above the table's DDL
  synonyms     the words people use for this table's contents. Search-only:
               they never reach the prompt, so be generous.
  columns      notes attached to individual columns in the DDL

A table missing from here is still indexed, from its DDL and any
`COMMENT ON` text alone -- so a new table works on day one and gets better
when someone writes its entry. `COMMENT ON TABLE/COLUMN` in the database is
read too, and is the better home once the schema is owned by a DBA.
"""
from __future__ import annotations

TABLES: dict[str, dict] = {
    # ------------------------------------------------------------------ ops
    "ops.sim_clock": {
        "description": "The simulation's current time (one row). Use "
                       "(SELECT now_ts FROM ops.sim_clock) wherever a query "
                       "needs 'now', 'today', 'last 7 days' -- never now().",
        "synonyms": "now current time today clock",
    },
    "ops.plants": {
        "description": "Bakery plants (sites). plant_code is how people name "
                       "them, e.g. TOR1, DAL1.",
        "synonyms": "plant site factory facility bakery location region city",
    },
    "ops.lines": {
        "description": "Production lines within a plant. People say 'Line 3 "
                       "at TOR1' meaning line_code 'L3' at plant TOR1; the "
                       "same line_code exists at several plants.",
        "synonyms": "line production line oven line capacity nameplate rate "
                    "throughput line type",
        "columns": {
            "line_code": "plant-local label, 'L1'..'L5'; pair with plant_id",
            "units_per_hour_nom": "nameplate rate; prefer line_sku_compat.units_per_hour for a specific SKU",
        },
    },
    "ops.skus": {
        "description": "Finished products (SKUs) with category, allergens, "
                       "shelf life and the proof window -- minutes staged "
                       "dough survives before it over-proofs into scrap.",
        "synonyms": "sku product item finished good flatbread artisan sweet "
                    "goods allergen shelf life proof window case pack",
        "columns": {
            "allergens": "text[]; test with 'milk' = ANY(allergens)",
            "proof_window_minutes": "staged dough becomes scrap after this many minutes",
        },
    },
    "ops.line_sku_compat": {
        "description": "Which lines can run which SKUs, at what line-specific "
                       "rate, and the changeover minutes to switch to that "
                       "SKU. Use for 'can line X make Y' and for rates.",
        "synonyms": "compatibility capable can run alternate line rate "
                    "changeover setup switch reallocate move",
    },
    "ops.production_runs": {
        "description": "Scheduled blocks of one SKU on one line with planned "
                       "vs actual units and times. A run is current when "
                       "planned_start <= now < planned_end.",
        "synonyms": "run production run schedule batch output planned actual "
                    "units produced volume aborted complete running yield "
                    "scrap rate denominator throughput attainment",
        "columns": {
            "actual_units": "units produced so far (0 until the run starts)",
        },
    },
    "ops.downtime_events": {
        "description": "Line stoppages with reason code and operator note. "
                       "ended_at IS NULL means the line is down right now; "
                       "duration_minutes is NULL while the event is open.",
        "synonyms": "downtime stoppage line down outage breakdown stopped "
                    "failure mechanical electrical changeover sanitation "
                    "material out staffing maintenance restore eta mttr",
        "columns": {
            "ended_at": "NULL = still down",
            "duration_minutes": "generated; NULL while open -- use now minus started_at for open events",
            "reason_detail": "free-text operator note, messy",
            "eta_minutes": "operator estimate of time to restore",
        },
    },
    "ops.wip_batches": {
        "description": "Staged proofed dough (work in progress) waiting for "
                       "the oven. Each batch expires at proof_expires_at and "
                       "becomes scrap if not baked by then.",
        "synonyms": "wip work in progress dough staged batch proofing "
                    "expiry expire over-proof perishable floor",
    },
    "ops.scrap_events": {
        "description": "Scrapped units by reason and source. source "
                       "'wip_expiry' is over-proofed dough; 'line' is normal "
                       "line loss. Scrap rate = scrap units / actual_units of "
                       "the runs -- aggregate scrap per run BEFORE joining to "
                       "runs, or the join double-counts.",
        "synonyms": "scrap waste loss reject yield scrap rate startup loss "
                    "overproof misshape underbake foreign material purge",
    },
    # ------------------------------------------------------------------ scm
    "scm.customers": {
        "description": "Customers with sales channel and service priority "
                       "tier (strategic > core > standard).",
        "synonyms": "customer client account buyer qsr grocery foodservice "
                    "priority tier strategic",
    },
    "scm.suppliers": {
        "description": "Raw material suppliers and their lead time in days.",
        "synonyms": "supplier vendor lead time",
    },
    "scm.materials": {
        "description": "Raw materials and ingredients (flour, butter, yeast, "
                       "packaging) with unit of measure and allergens.",
        "synonyms": "material ingredient raw material flour sugar butter "
                    "yeast packaging uom",
    },
    "scm.sku_bom": {
        "description": "Bill of materials: quantity of each material one unit "
                       "of a SKU consumes. Multiply by planned units for "
                       "material requirements.",
        "synonyms": "bom bill of materials recipe formula consumption usage "
                    "requirement",
    },
    "scm.inventory_lots": {
        "description": "Raw material stock on hand by lot and plant. Only "
                       "status 'available' is usable.",
        "synonyms": "inventory stock on hand lot quantity warehouse held "
                    "available expiry",
    },
    "scm.purchase_orders": {
        "description": "Inbound purchase orders for materials. promised_at is "
                       "the contract date, eta_at the carrier's current "
                       "estimate; eta_at > promised_at means it is late.",
        "synonyms": "purchase order po inbound delivery supplier late slip "
                    "eta promised open in transit received expedite",
    },
    "scm.customer_orders": {
        "description": "Customer order headers with promised ship time and "
                       "status. Quantities are on customer_order_lines.",
        "synonyms": "customer order sales order so shipment ship date due "
                    "open shipped short cancelled",
    },
    "scm.customer_order_lines": {
        "description": "Order lines: SKU, quantity ordered and quantity "
                       "fulfilled. Shortfall = qty_units - qty_fulfilled.",
        "synonyms": "order line quantity ordered fulfilled shortfall demand "
                    "units",
    },
    "scm.run_order_allocations": {
        "description": "Which production run supplies which order line, and "
                       "how many units. The only correct way to connect a "
                       "run (or a stopped line) to the customers it serves.",
        "synonyms": "allocation run to order which orders does a run supply "
                    "commitment at risk impact",
    },
    # ------------------------------------------------------------------ qms
    "qms.quality_holds": {
        "description": "Quality holds on a SKU or a material lot at a plant. "
                       "status 'open' means the product or lot is blocked.",
        "synonyms": "quality hold qa hold blocked quarantine released "
                    "rejected investigation",
    },
    # ------------------------------------------------------------------ act
    "act.principals": {
        "description": "People who use PROOF, their role and plant scope. "
                       "Empty plant_scope means every plant.",
        "synonyms": "user person principal role plant manager supervisor "
                    "director operator who",
    },
    "act.proposed_actions": {
        "description": "Actions the agent proposed for human approval "
                       "(reallocate run, notify customer, expedite PO, hold "
                       "product) and their decision status.",
        "synonyms": "proposal proposed action approval queue pending "
                    "approved rejected executed expired decision",
        "columns": {
            "payload": "jsonb action details; read with payload->>'key'",
        },
    },
    "act.decision_log": {
        "description": "Append-only history of proposals: who proposed, "
                       "approved, rejected or executed each action and when.",
        "synonyms": "audit log history decision who approved when",
    },
    "act.authz_denials": {
        "description": "Authorization attempts that were refused: who tried "
                       "what, at which plant, and why it was denied.",
        "synonyms": "denied refused unauthorized permission authorization "
                    "security attempt",
    },
}
