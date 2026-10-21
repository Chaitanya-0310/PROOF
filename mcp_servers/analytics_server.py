"""Analytics MCP server -- ad-hoc questions no purpose-built tool anticipated.

Every other server exposes queries a person wrote. This one exposes the
schema itself, one routed slice at a time, and a guarded way to run a SELECT
against it. The sub-agent on the other end writes the SQL.

Like every server here it holds no model credentials: it routes and it
executes, and the model that writes the query lives in the agent process.
Its connection is `agent_read` in a read-only transaction -- see
proof/text2sql/guard.py for the four layers between a generated query and
the plant data.
"""
from __future__ import annotations

from _serve import build, text_result

from proof.text2sql import guard, router
from proof.text2sql.prompts import SQL_RULES

server = build(
    "proof-analytics",
    "Ad-hoc read-only SQL over the plant database (ops = MES, scm = ERP, "
    "qms = quality, act = approval queue). Workflow: call find_tables with "
    "the question to get the few relevant tables' DDL; write ONE PostgreSQL "
    "SELECT against only those tables; call run_sql. If run_sql returns an "
    "error, read it, fix the query and run it again. If a table you need was "
    "not returned, call describe_tables for it rather than guessing its "
    "columns. Quote figures from the rows and always report the SQL.\n\n"
    "SQL rules:\n" + SQL_RULES,
)


@server.tool()
def find_tables(question: str, top_k: int = router.DEFAULT_TOP_K) -> str:
    """The tables a question needs, with their DDL, column meanings and join keys.

    Retrieval picks the top_k most relevant tables, then adds the tables that
    sit on the join path between them, plus the simulation clock.

    Args:
        question: The question in plain English, as asked.
        top_k: Tables to retrieve before join-path expansion (2-8).
    """
    r = router.route(question, max(2, min(top_k, 8)))
    # No `sql` field: this is schema lookup, not plant evidence, and the
    # coordinator's evidence section should show only queries that produced
    # numbers.
    return text_result({
        "rows": [{"table": t.name, "via": t.via, "ddl": t.ddl} for t in r.tables],
        "row_count": len(r.tables),
        "note": (f"{len(r.tables)} of {r.total_tables} tables. 'bridge' tables "
                 "are on the join path between retrieved ones; use them to join "
                 "rather than joining on look-alike column names."),
    })


@server.tool()
def describe_tables(tables: list[str]) -> str:
    """Full DDL for specific tables, by schema-qualified name.

    Use when find_tables did not return a table the query needs.

    Args:
        tables: Names like ["scm.sku_bom", "ops.line_sku_compat"].
    """
    return text_result(router.describe(tables))


@server.tool()
def run_sql(sql: str) -> str:
    """Run one read-only SELECT and return its rows and the SQL.

    Refused before execution if it is not a single SELECT/WITH, uses a write
    or DDL keyword, or reads outside ops/scm/qms/act. Runs as a read-only
    role with a 5 s timeout and returns at most 500 rows; `truncated: true`
    means row_count is NOT a total. On failure the result has `error` with
    the database's own message -- fix the query and call again.

    Args:
        sql: A single PostgreSQL SELECT statement.
    """
    return text_result(guard.run(sql))


if __name__ == "__main__":
    server.run("stdio")
