"""Quality domain tools -- holds and allergen rules.

Phase 2 scope is the STRUCTURED half of quality only. The procedural half --
"what does the L3 restart validation SOP actually require?" -- is RAG over the
SOP corpus and arrives in Phase 3, where citations become mandatory.

Keeping the split explicit matters: allergen compatibility is a hard rule that
belongs in SQL, where it is checkable, not in a retrieved document where the
model could paraphrase it wrong. In food manufacturing an allergen decision
that is 95% right is not a good score, it is a recall.
"""
from __future__ import annotations

from ._base import query, sim_now_sql

NOW = sim_now_sql()


def get_open_quality_holds(plant_code: str) -> dict:
    """Open QA holds at a plant, with what they are blocking."""
    return query(f"""
        SELECT h.hold_id, p.plant_code, k.sku_code, k.name AS sku_name,
               i.lot_code, m.name AS material_name,
               h.reason, h.status, h.opened_at,
               round(EXTRACT(EPOCH FROM ({NOW} - h.opened_at)) / 3600, 1)
                   AS hours_open
        FROM qms.quality_holds h
        JOIN ops.plants p ON p.plant_id = h.plant_id
        LEFT JOIN ops.skus k          ON k.sku_id = h.sku_id
        LEFT JOIN scm.inventory_lots i ON i.lot_id = h.lot_id
        LEFT JOIN scm.materials m      ON m.material_id = i.material_id
        WHERE p.plant_code = %s AND h.status = 'open'
        ORDER BY h.opened_at
    """, (plant_code,))


def check_allergen_compatibility(from_line_id: int, to_sku_id: int) -> dict:
    """Can `to_sku_id` follow what `from_line_id` is currently running?

    The rule modelled here: introducing an allergen the line was not already
    running requires a full wet allergen wash, not a dry changeover. So the
    direction matters. Moving from a milk+egg product to a wheat-only one is
    cheap; the reverse is not.

    `added_allergens` is returned rather than a bare boolean so the agent can
    say WHICH allergen forces the wash -- which is what a quality manager will
    ask the moment the recommendation lands.
    """
    return query(f"""
        WITH current_sku AS (
            SELECT k.sku_id, k.sku_code, k.name, k.allergens
            FROM ops.production_runs r
            JOIN ops.skus k ON k.sku_id = r.sku_id
            WHERE r.line_id = %s
              AND r.planned_start <= {NOW} AND r.planned_end > {NOW}
            LIMIT 1
        ),
        target AS (
            SELECT sku_id, sku_code, name, allergens
            FROM ops.skus WHERE sku_id = %s
        )
        SELECT c.sku_code AS current_sku, c.name AS current_sku_name,
               c.allergens AS current_allergens,
               t.sku_code AS target_sku, t.name AS target_sku_name,
               t.allergens AS target_allergens,
               ARRAY(SELECT unnest(t.allergens)
                     EXCEPT SELECT unnest(c.allergens)) AS added_allergens,
               (ARRAY(SELECT unnest(t.allergens)
                      EXCEPT SELECT unnest(c.allergens)) = '{{}}')
                   AS dry_changeover_sufficient,
               COALESCE(co.changeover_minutes, 0) AS scheduled_changeover_minutes
        FROM current_sku c
        CROSS JOIN target t
        LEFT JOIN ops.line_sku_compat co
               ON co.line_id = %s AND co.sku_id = t.sku_id
    """, (from_line_id, to_sku_id, from_line_id),
        note="If dry_changeover_sufficient is false, a full wet allergen wash "
             "is required and the real changeover will exceed "
             "scheduled_changeover_minutes. Phase 3 retrieves the governing "
             "SOP for the exact procedure.")


def get_sku_allergens(sku_code: str | None = None,
                      category: str | None = None) -> dict:
    """Allergen profile and proof window for SKUs."""
    return query("""
        SELECT sku_code, name AS sku_name, category, allergens,
               shelf_life_days, units_per_case,
               requires_proofing, proof_window_minutes
        FROM ops.skus
        WHERE (%s::text IS NULL OR sku_code = %s)
          AND (%s::text IS NULL OR category = %s)
        ORDER BY category, sku_code
    """, (sku_code, sku_code, category, category),
        note="proof_window_minutes is how long staged dough survives after "
             "staging. Shorter windows mean a stoppage destroys product faster.")
