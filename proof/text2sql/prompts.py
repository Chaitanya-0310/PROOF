"""The SQL-writing rules, shared by both generators.

The CLI pipeline (pipeline.py) and the analytics agent (via the MCP server's
instructions) write SQL under exactly these rules. Kept free of imports so
the MCP server can load it without pulling in the model SDK -- a server
process has no business holding model client code.
"""
from __future__ import annotations

SQL_RULES = """\
- Use ONLY the tables and columns in the schema provided. Schema-qualify every
  table (ops.lines, never lines).
- One read-only statement: SELECT, optionally with CTEs. No INSERT, UPDATE,
  DELETE, DDL, SELECT INTO, or second statement.
- "Now" is the simulation clock: (SELECT now_ts FROM ops.sim_clock). Never use
  now(), current_timestamp or current_date -- the data is anchored to the
  clock, so wall-clock time returns nothing.
- Compute counts, totals, averages and rates in SQL. Never rely on counting
  returned rows.
- When joining a parent to two child tables (runs to scrap AND to downtime),
  aggregate each child per parent in a CTE first; joining both raw multiplies
  rows and silently inflates every sum.
- Text values are case-sensitive and must match the listed values exactly
  ('open', 'MECHANICAL').
- Output readable codes (plant_code, line_code, sku_code, order_code,
  customer name) rather than bare internal ids.
- Lists: order meaningfully and LIMIT 100 unless the question asks for all.
- Timestamps are timestamptz. For "last 7 days" use
  >= (SELECT now_ts FROM ops.sim_clock) - interval '7 days'."""
