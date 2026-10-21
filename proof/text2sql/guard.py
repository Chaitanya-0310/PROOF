"""Run SQL a model wrote, safely.

Every other query in PROOF was written and reviewed by a person. These were
not, and the question that produced them may have been written by anyone, so
nothing here trusts the text. Four layers, each sufficient for something on
its own and none relied on alone:

1. **The role.** `agent_read` holds SELECT and nothing else (03_roles.sh).
   A DELETE that got past every check below would still fail on grants.
   This is the control that matters; the rest narrow what a read can do.

2. **A read-only transaction with a statement timeout.** Catches anything
   the role might one day be granted by mistake, and bounds the cost of a
   cartesian join or a pg_sleep to seconds.

3. **A static check.** One statement, starting SELECT or WITH, with no
   write/DDL keyword and none of the functions that read files, sleep, or
   run a query passed in as a string (query_to_xml would run SQL that the
   relation check below never sees).

4. **Relation allow-list from EXPLAIN.** The planner, not a regex, says
   which tables the query touches -- through CTEs, subqueries and aliases --
   and anything outside the plant schemas is refused before execution.
   A regex over the text cannot do this reliably; the planner already has.

Results are capped by fetching through a server-side cursor rather than by
rewriting the query with LIMIT, which would change the meaning of an ORDER
BY ... LIMIT the model wrote itself. A capped result says so loudly, because
"500 rows" read as "500 orders" is a bug this project has already had once.
"""
from __future__ import annotations

import re
from typing import Any

import psycopg

from proof.db import connect
from proof.tools._base import _jsonable, _tidy

from .catalog import QUERYABLE_SCHEMAS

MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 5000

_FORBIDDEN_KEYWORDS = re.compile(r"\b(" + "|".join([
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "GRANT", "REVOKE", "COPY", "CALL", "DO", "LOCK", "VACUUM",
    "ANALYZE", "REINDEX", "CLUSTER", "REFRESH", "SET", "RESET", "LISTEN",
    "NOTIFY", "UNLISTEN", "PREPARE", "EXECUTE", "DEALLOCATE", "DISCARD",
    "COMMENT", "SECURITY", "INTO", "IMPORT", "LOAD", "CHECKPOINT",
]) + r")\b", re.I)

_FORBIDDEN_FUNCTIONS = re.compile(r"\b(" + "|".join([
    r"pg_sleep\w*", r"pg_read_\w+", r"pg_ls_\w+", r"pg_stat_file",
    r"lo_\w+", r"dblink\w*", r"pg_terminate_backend", r"pg_cancel_backend",
    r"set_config", r"pg_advisory\w*", r"pg_notify", r"\w*to_xml\w*",
    r"ts_stat", r"ts_rewrite", r"pg_reload_conf", r"pg_rotate_logfile",
]) + r")\s*\(", re.I)


class UnsafeSQL(ValueError):
    """The query was refused before it reached the database."""


def check(sql: str) -> str:
    """Static check. Returns the query with trailing semicolons removed."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise UnsafeSQL("empty query")
    scrubbed = _scrub(text)
    if ";" in scrubbed:
        raise UnsafeSQL("only one statement is allowed")
    first = scrubbed.lstrip("( \n\t").split(None, 1)[0].upper() if scrubbed.strip() else ""
    if first not in ("SELECT", "WITH"):
        raise UnsafeSQL(f"query must start with SELECT or WITH, not {first or 'nothing'}")
    m = _FORBIDDEN_KEYWORDS.search(scrubbed)
    if m:
        raise UnsafeSQL(f"{m.group(1).upper()} is not allowed in a read-only query")
    m = _FORBIDDEN_FUNCTIONS.search(scrubbed)
    if m:
        raise UnsafeSQL(f"function {m.group(1)}() is not allowed")
    return text


def _scrub(sql: str) -> str:
    """Blank out comments, string literals and quoted identifiers.

    So that a keyword check does not fire on 'DELETE' inside a string, and --
    the direction that matters -- so a statement cannot hide after a
    semicolon inside a comment the check would otherwise skip.
    """
    out, i, n = [], 0, len(sql)
    while i < n:
        ch, nxt = sql[i], sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            if j < 0:
                raise UnsafeSQL("unterminated comment")
            i = j + 2
            out.append(" ")
        elif ch in ("'", '"'):
            j = i + 1
            while True:
                j = sql.find(ch, j)
                if j < 0:
                    raise UnsafeSQL("unterminated quote")
                if sql[j + 1:j + 2] == ch:      # '' or "" escape
                    j += 2
                    continue
                break
            out.append(" '' " if ch == "'" else ' "x" ')
            i = j + 1
        elif ch == "$":
            m = re.match(r"\$(\w*)\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                if j < 0:
                    raise UnsafeSQL("unterminated dollar quote")
                out.append(" '' ")
                i = j + len(tag)
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# A dedicated connection, separate from the tools' shared one: this one is
# switched to read-only mode, and a failed model query must not leave the
# hand-written tools' connection in an aborted transaction.
_conn: psycopg.Connection | None = None


def _get_conn() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = connect("agent_read")
        _conn.read_only = True          # every transaction is BEGIN READ ONLY
    return _conn


def run(sql: str, *, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Check, plan, allow-list and execute. Errors come back as data.

    Returned as data rather than raised so the caller -- the repair loop or
    the analytics agent -- can hand the database's own error message back to
    the model, which is what lets it fix a wrong column name on the next try.
    """
    from proof.trace import set_io, span  # local import, as in tools._base

    shown = _tidy(sql)
    try:
        text = check(sql)
    except UnsafeSQL as exc:
        return {"error": f"refused: {exc}", "error_kind": "refused", "sql": shown,
                "rows": [], "row_count": 0}

    conn = _get_conn()
    with span("sql", "sql", sql=shown, origin="text2sql") as sp:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
                cur.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + text)
                plan = cur.fetchone()[0]
            touched = sorted(_relations(plan))
            outside = [t for t in touched if t.split(".")[0] not in QUERYABLE_SCHEMAS]
            if outside:
                conn.rollback()
                return {"error": f"refused: query reads {', '.join(outside)}, outside "
                                 f"the queryable schemas ({', '.join(QUERYABLE_SCHEMAS)})",
                        "error_kind": "refused", "sql": shown, "rows": [], "row_count": 0}

            with conn.cursor(name="text2sql") as cur:
                cur.execute(text)
                cols = [d.name for d in cur.description]
                raw = cur.fetchmany(max_rows + 1)
            conn.rollback()
        except psycopg.Error as exc:
            conn.rollback()
            diag = getattr(exc, "diag", None)
            msg = (diag.message_primary if diag and diag.message_primary else str(exc)).strip()
            hint = diag.message_hint if diag else None
            return {"error": msg + (f" (hint: {hint})" if hint else ""),
                    "error_kind": "database", "sqlstate": exc.sqlstate,
                    "sql": shown, "rows": [], "row_count": 0}

        truncated = len(raw) > max_rows
        rows = [{c: _jsonable(v) for c, v in zip(cols, r)} for r in raw[:max_rows]]
        sp.set_attribute("proof.row_count", len(rows))
        set_io(sp, input={"sql": shown}, output={"row_count": len(rows),
                                                 "truncated": truncated,
                                                 "rows": rows[:50]})

    out: dict[str, Any] = {"rows": rows, "row_count": len(rows), "sql": shown,
                           "params": [], "tables": touched, "truncated": truncated}
    if truncated:
        out["note"] = (f"TRUNCATED: more than {max_rows} rows matched and only the first "
                       f"{max_rows} are shown. row_count is NOT a total -- compute counts "
                       f"and sums in SQL (count(*), sum()) instead of counting rows.")
    return out


def _relations(plan: Any) -> set[str]:
    """Every schema-qualified relation in an EXPLAIN (VERBOSE, FORMAT JSON) plan."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "Relation Name" in node:
                found.add(f"{node.get('Schema', '?')}.{node['Relation Name']}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return found
