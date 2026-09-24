"""Production domain tools -- what the lines are doing and what stopping costs.

Stands in for MES/SCADA. The Production Agent gets these and nothing else.
That narrowness is the point: an agent with six well-chosen tools picks
correctly far more reliably than one with thirty.
"""
from __future__ import annotations

from ._base import query, sim_now_sql

NOW = sim_now_sql()


def get_open_downtime(plant_code: str | None = None) -> dict:
    """Every line that is stopped RIGHT NOW, with elapsed time and restore ETA.

    `ended_at IS NULL` is the open-event convention the whole system rests on.
    """
    return query(f"""
        SELECT p.plant_code, l.line_code, l.name AS line_name, d.event_id,
               d.line_id, d.run_id, d.reason_code, d.reason_detail,
               d.started_at,
               round(EXTRACT(EPOCH FROM ({NOW} - d.started_at)) / 60)::int
                   AS minutes_down_so_far,
               d.eta_minutes AS operator_eta_minutes
        FROM ops.downtime_events d
        JOIN ops.lines l  ON l.line_id = d.line_id
        JOIN ops.plants p ON p.plant_id = l.plant_id
        WHERE d.ended_at IS NULL
          AND (%s::text IS NULL OR p.plant_code = %s)
        ORDER BY d.started_at
    """, (plant_code, plant_code))


def get_line_status(line_id: int) -> dict:
    """What a line is running now: SKU, rate, planned vs actual, schedule."""
    return query(f"""
        SELECT p.plant_code, l.line_code, l.name AS line_name, l.line_type,
               r.run_id, k.sku_code, k.name AS sku_name, k.category,
               k.allergens, k.proof_window_minutes,
               c.units_per_hour AS line_rate_units_per_hour,
               r.planned_units, r.actual_units, r.status,
               r.planned_start, r.planned_end
        FROM ops.lines l
        JOIN ops.plants p ON p.plant_id = l.plant_id
        LEFT JOIN ops.production_runs r
               ON r.line_id = l.line_id
              AND r.planned_start <= {NOW} AND r.planned_end > {NOW}
        LEFT JOIN ops.skus k ON k.sku_id = r.sku_id
        LEFT JOIN ops.line_sku_compat c
               ON c.line_id = l.line_id AND c.sku_id = r.sku_id
        WHERE l.line_id = %s
    """, (line_id,))


def estimate_output_loss(line_id: int, minutes_down: int) -> dict:
    """Units NOT produced if `line_id` stays down for `minutes_down`.

    Uses the line-specific rate for the SKU actually running, not the line's
    nameplate. Those differ by up to 22% in this dataset, and using nameplate
    would overstate every loss estimate the agent ever gives.
    """
    return query(f"""
        SELECT l.line_code, k.sku_code, k.name AS sku_name,
               c.units_per_hour AS line_rate_units_per_hour,
               %s AS minutes_down,
               round(c.units_per_hour * %s / 60.0)::int AS units_not_produced,
               round(c.units_per_hour * %s / 60.0 / k.units_per_case)::int
                   AS cases_not_produced
        FROM ops.lines l
        JOIN ops.production_runs r
          ON r.line_id = l.line_id
         AND r.planned_start <= {NOW} AND r.planned_end > {NOW}
        JOIN ops.skus k ON k.sku_id = r.sku_id
        JOIN ops.line_sku_compat c
          ON c.line_id = l.line_id AND c.sku_id = r.sku_id
        WHERE l.line_id = %s
    """, (minutes_down, minutes_down, minutes_down, line_id),
        note="Output loss only. Does NOT include staged WIP that expires while "
             "the line is stopped -- ask the inventory agent for that; on a "
             "proofed product it is usually the larger number.")


def find_alternate_lines(sku_id: int, plant_code: str) -> dict:
    """Lines in the same plant that could run this SKU, and what switching costs.

    `busy_until` is NULL when the line is genuinely free. Changeover minutes
    are real: proposing a move without them would understate the cost of the
    recommendation the agent is about to make.
    """
    return query(f"""
        SELECT p.plant_code, l.line_id, l.line_code, l.name AS line_name,
               c.units_per_hour AS line_rate_units_per_hour,
               c.changeover_minutes,
               r.run_id        AS currently_running_run,
               r.planned_end   AS busy_until,
               EXISTS (SELECT 1 FROM ops.downtime_events d
                       WHERE d.line_id = l.line_id AND d.ended_at IS NULL)
                   AS is_currently_down
        FROM ops.line_sku_compat c
        JOIN ops.lines l  ON l.line_id = c.line_id
        JOIN ops.plants p ON p.plant_id = l.plant_id
        LEFT JOIN ops.production_runs r
               ON r.line_id = l.line_id
              AND r.planned_start <= {NOW} AND r.planned_end > {NOW}
              AND r.status IN ('running', 'scheduled')
        WHERE c.sku_id = %s AND p.plant_code = %s
        ORDER BY (r.run_id IS NULL) DESC, c.changeover_minutes
    """, (sku_id, plant_code),
        note="A line is available if currently_running_run IS NULL and "
             "is_currently_down is false. Any move costs changeover_minutes "
             "before the first good unit.")


def get_scrap_breakdown(days: int = 30, plant_code: str | None = None,
                        group_by: str = "line") -> dict:
    """Scrap rate over a window, sliced by line, SKU, category or reason code.

    CAREFUL -- scrap is aggregated to one row per run BEFORE joining to runs.
    Joining ops.scrap_events straight to ops.production_runs counts a run's
    planned_units once per scrap row, so the denominator inflates exactly when
    scrap rises and a deterioration reads as an improvement. That bug shipped
    once in this repo (see README) and the fix lives here permanently.
    """
    dims = {
        "line": ("p.plant_code || '/' || l.line_code", "line"),
        "sku": ("k.sku_code || ' ' || k.name", "sku"),
        "category": ("k.category", "category"),
        "reason": ("COALESCE(s.reason_code, 'NO_SCRAP')", "reason_code"),
    }
    if group_by not in dims:
        return {"rows": [], "row_count": 0, "sql": "",
                "error": f"group_by must be one of {sorted(dims)}"}
    expr, label = dims[group_by]

    # The reason slice needs scrap rows un-aggregated (a run has many reasons),
    # so it reports share-of-scrap instead of a rate. Reporting a "rate" per
    # reason would silently reintroduce the fan-out bug described above.
    if group_by == "reason":
        return query(f"""
            SELECT s.reason_code, s.source,
                   SUM(s.units)::int AS scrap_units,
                   round(100.0 * SUM(s.units) / SUM(SUM(s.units)) OVER (), 2)
                       AS pct_of_all_scrap,
                   count(*) AS events
            FROM ops.scrap_events s
            JOIN ops.lines l  ON l.line_id = s.line_id
            JOIN ops.plants p ON p.plant_id = l.plant_id
            WHERE s.occurred_at > {NOW} - make_interval(days => %s)
              AND (%s::text IS NULL OR p.plant_code = %s)
            GROUP BY s.reason_code, s.source
            ORDER BY scrap_units DESC
        """, (days, plant_code, plant_code),
            note="Share of total scrap, not a rate. A rate per reason code is "
                 "not well defined because one run carries several reasons.")

    return query(f"""
        WITH s AS (
            SELECT run_id, SUM(units) AS scrap_units
            FROM ops.scrap_events GROUP BY run_id
        )
        SELECT {expr} AS {label},
               count(*)                          AS runs,
               SUM(r.planned_units)::int         AS planned_units,
               SUM(COALESCE(s.scrap_units, 0))::int AS scrap_units,
               round(100.0 * SUM(COALESCE(s.scrap_units, 0))
                     / NULLIF(SUM(r.planned_units), 0), 2) AS scrap_pct
        FROM ops.production_runs r
        JOIN ops.lines l  ON l.line_id = r.line_id
        JOIN ops.plants p ON p.plant_id = l.plant_id
        JOIN ops.skus k   ON k.sku_id = r.sku_id
        LEFT JOIN s ON s.run_id = r.run_id
        WHERE r.planned_start > {NOW} - make_interval(days => %s)
          AND r.planned_end < {NOW}
          AND (%s::text IS NULL OR p.plant_code = %s)
        GROUP BY 1
        HAVING SUM(r.planned_units) > 0
        ORDER BY scrap_pct DESC
    """, (days, plant_code, plant_code))


def compare_scrap_windows(line_id: int, recent_days: int = 30,
                          baseline_days: int = 90) -> dict:
    """Scrap and downtime for a line, recent window vs the baseline before it.

    Built for root-cause questions ("why is scrap up?"). It returns changeover
    counts alongside the scrap rate, because on this dataset those two move
    together and the agent should be able to see the correlation rather than
    be told about it.
    """
    return query(f"""
        WITH r AS (
            SELECT run_id, planned_units,
                   CASE WHEN planned_start > {NOW} - make_interval(days => %s)
                        THEN 'recent' ELSE 'baseline' END AS bucket
            FROM ops.production_runs
            WHERE line_id = %s
              AND planned_start > {NOW} - make_interval(days => %s)
              AND planned_end < {NOW}
        ),
        s AS (SELECT run_id, SUM(units) AS scrap_units
              FROM ops.scrap_events GROUP BY run_id),
        d AS (SELECT run_id,
                     count(*) FILTER (WHERE reason_code = 'CHANGEOVER') AS changeovers,
                     count(*)                                           AS downtime_events,
                     COALESCE(SUM(duration_minutes), 0)                 AS downtime_minutes
              FROM ops.downtime_events GROUP BY run_id)
        SELECT r.bucket,
               count(*)                                       AS runs,
               round(avg(COALESCE(d.changeovers, 0)), 2)      AS changeovers_per_run,
               round(avg(COALESCE(d.downtime_minutes, 0)), 1) AS downtime_min_per_run,
               round(100.0 * SUM(COALESCE(s.scrap_units, 0))
                     / NULLIF(SUM(r.planned_units), 0), 2)    AS scrap_pct
        FROM r LEFT JOIN s USING (run_id) LEFT JOIN d USING (run_id)
        GROUP BY r.bucket ORDER BY r.bucket
    """, (recent_days, line_id, baseline_days),
        note="The two buckets are DISJOINT: 'recent' is the last recent_days, "
             "'baseline' is everything from baseline_days ago up to the start "
             "of that recent window. Compare the rows directly -- baseline is "
             "not a superset, so a difference is a real change, not dilution.")
