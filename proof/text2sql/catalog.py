"""Introspect the live schema and render each table as enriched DDL.

The DDL a model sees is built from the database, not copied from db/init.
A migration that adds a column shows up at the next `make sql-index` with no
edit here, and the prompt can never describe a column that does not exist.

What the raw catalogue lacks, this adds next to each column:
  - CHECK constraints rendered as "one of: 'open','shipped',..." -- a model
    that has to guess 'OPEN' vs 'open' writes a query that returns nothing
    and looks like a real answer;
  - foreign keys as "-> ops.lines.line_id", so join paths are explicit;
  - the observed values of low-cardinality text columns and example codes
    ('TOR1', 'SO-100119'), which is also what lets the router match a
    question that names a plant rather than a table;
  - the curated business notes from descriptions.py and any COMMENT ON text.
"""
from __future__ import annotations

import re
import heapq
from collections import defaultdict
from dataclasses import dataclass, field

from psycopg import sql as pgsql

from .descriptions import TABLES as CURATED

# The plant systems plus the approval queue. docs.* is deliberately absent:
# SOP text is answered by retrieval with citations, not by SELECT, and an
# embedding column in a prompt is pure noise. meta.* is this feature's own
# index.
QUERYABLE_SCHEMAS = ("ops", "scm", "qms", "act")

# Always in the prompt, whatever retrieval returns.
PINNED = ("ops.sim_clock",)

# Low-cardinality text columns get their full value list; *_code columns get
# a few examples. Free text is skipped: sampling operator notes would put
# noise into the prompt and nothing useful into search.
ENUM_MAX_DISTINCT = 12
CODE_EXAMPLES = 3
FREE_TEXT = {"reason_detail", "rationale", "decision_note", "detail",
             "source_question", "name", "display_name", "reason", "city"}


@dataclass
class Column:
    name: str
    type: str
    not_null: bool
    default: str | None
    generated: bool
    comment: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ForeignKey:
    table: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]

    def as_dict(self) -> dict:
        return {"from": self.table, "from_cols": self.columns,
                "to": self.ref_table, "to_cols": self.ref_columns}


@dataclass
class Table:
    name: str                       # 'ops.downtime_events'
    schema: str
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    referenced_by: list[ForeignKey] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    uniques: list[list[str]] = field(default_factory=list)
    comment: str | None = None
    row_count: int | None = None

    @property
    def description(self) -> str:
        curated = CURATED.get(self.name, {}).get("description")
        return curated or self.comment or f"Table {self.name}."

    @property
    def neighbours(self) -> list[str]:
        return sorted(({fk.ref_table for fk in self.foreign_keys}
                       | {fk.table for fk in self.referenced_by}) - {self.name})

    def ddl(self) -> str:
        """The enriched DDL block injected into the generation prompt."""
        fk_by_col = {fk.columns[0]: fk for fk in self.foreign_keys
                     if len(fk.columns) == 1}
        rows = f" (~{self.row_count:,} rows)" if self.row_count is not None else ""
        out = [f"-- {self.name}{rows}: {self.description}",
               f"CREATE TABLE {self.name} ("]
        body: list[str] = []
        for c in self.columns:
            line = f"  {c.name} {c.type}"
            if c.not_null:
                line += " NOT NULL"
            if c.generated:
                line += " GENERATED"
            notes = []
            if c.name in fk_by_col:
                fk = fk_by_col[c.name]
                notes.append(f"-> {fk.ref_table}.{fk.ref_columns[0]}")
            notes.extend(c.notes)
            if c.comment:
                notes.append(c.comment)
            body.append((line, notes))
        if self.primary_key:
            body.append((f"  PRIMARY KEY ({', '.join(self.primary_key)})", []))
        for fk in self.foreign_keys:
            if len(fk.columns) > 1:
                body.append((f"  FOREIGN KEY ({', '.join(fk.columns)}) REFERENCES "
                             f"{fk.ref_table} ({', '.join(fk.ref_columns)})", []))
        for u in self.uniques:
            body.append((f"  UNIQUE ({', '.join(u)})", []))
        for i, (line, notes) in enumerate(body):
            sep = "," if i < len(body) - 1 else ""
            out.append(f"{line}{sep}" + (f"  -- {'; '.join(notes)}" if notes else ""))
        out.append(");")
        if self.referenced_by:
            refs = ", ".join(sorted({f"{fk.table}.{fk.columns[0]}"
                                     for fk in self.referenced_by}))
            out.append(f"-- referenced by: {refs}")
        return "\n".join(out)

    def search_text(self) -> str:
        """What retrieval matches: meaning first, then vocabulary and values."""
        curated = CURATED.get(self.name, {})
        parts = [self.description, curated.get("synonyms", "")]
        if self.comment and self.comment != self.description:
            parts.append(self.comment)
        parts.append("columns: " + " ".join(
            c.name.replace("_", " ") for c in self.columns))
        # Enum values and sample codes are what match a question that names a
        # plant ('TOR1') or a status ('MATERIAL_OUT') rather than a table.
        for c in self.columns:
            parts.extend(c.notes)
            if c.comment:
                parts.append(c.comment)
        return "\n".join(p for p in parts if p)


def load_catalog(conn, schemas: tuple[str, ...] = QUERYABLE_SCHEMAS,
                 *, sample_values: bool = True) -> dict[str, Table]:
    """Read every table in `schemas` from pg_catalog.

    pg_catalog rather than information_schema: the latter hides constraints
    on tables the role does not own, which for a least-privilege reader like
    agent_read is all of them.
    """
    tables: dict[str, Table] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT n.nspname, c.relname, obj_description(c.oid, 'pg_class')
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm') AND n.nspname = ANY(%s)
            ORDER BY n.nspname, c.relname
        """, (list(schemas),))
        for nsp, rel, comment in cur.fetchall():
            tables[f"{nsp}.{rel}"] = Table(name=f"{nsp}.{rel}", schema=nsp,
                                           comment=comment)

        cur.execute("""
            SELECT n.nspname || '.' || c.relname, a.attname,
                   format_type(a.atttypid, a.atttypmod), a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid), a.attgenerated <> '',
                   col_description(c.oid, a.attnum)
            FROM pg_attribute a
            JOIN pg_class c     ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE n.nspname = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
              AND c.relkind IN ('r', 'p', 'v', 'm')
            ORDER BY 1, a.attnum
        """, (list(schemas),))
        for tname, col, typ, nn, default, gen, comment in cur.fetchall():
            if tname not in tables:
                continue
            # The embedding column of a vector table is never useful in SQL
            # a model writes, and its type string invites it to try.
            if typ.startswith(("vector", "tsvector")):
                continue
            tables[tname].columns.append(Column(col, typ, nn, default, gen, comment))

        cur.execute("""
            SELECT con.contype,
                   sn.nspname || '.' || sc.relname,
                   ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n
                         ORDER BY k.o),
                   tn.nspname || '.' || tc.relname,
                   ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n
                         ORDER BY k.o),
                   pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class sc      ON sc.oid = con.conrelid
            JOIN pg_namespace sn  ON sn.oid = sc.relnamespace
            LEFT JOIN pg_class tc     ON tc.oid = con.confrelid
            LEFT JOIN pg_namespace tn ON tn.oid = tc.relnamespace
            WHERE sn.nspname = ANY(%s) AND con.contype IN ('p', 'f', 'u', 'c')
            ORDER BY con.conname
        """, (list(schemas),))
        for kind, tname, cols, ref, ref_cols, definition in cur.fetchall():
            t = tables.get(tname)
            if t is None:
                continue
            if kind == "p":
                t.primary_key = list(cols)
            elif kind == "u":
                t.uniques.append(list(cols))
            elif kind == "c":
                t.checks.append(definition)
            elif kind == "f" and ref in tables:
                fk = ForeignKey(tname, list(cols), ref, list(ref_cols))
                t.foreign_keys.append(fk)
                tables[ref].referenced_by.append(fk)

        for t in tables.values():
            cur.execute(pgsql.SQL("SELECT count(*) FROM {}").format(
                pgsql.Identifier(*t.name.split("."))))
            t.row_count = cur.fetchone()[0]
            _annotate(cur, t, sample_values)
    conn.rollback()
    return tables


# CHECK (status = ANY (ARRAY['open'::text, 'shipped'::text]))  or
# CHECK (status IN (...)) as pg_get_constraintdef renders it.
_CHECK_ENUM = re.compile(r"\(\(?(\w+)\)?\s*=\s*ANY\s*\(\(?ARRAY\[(.+?)\]")


def _annotate(cur, t: Table, sample_values: bool) -> None:
    """Attach enum lists, sample codes and curated notes to columns."""
    curated_cols = CURATED.get(t.name, {}).get("columns", {})
    enums: dict[str, list[str]] = {}
    for definition in t.checks:
        m = _CHECK_ENUM.search(definition)
        if m:
            enums[m.group(1)] = re.findall(r"'([^']*)'", m.group(2))

    for c in t.columns:
        if c.name in enums:
            c.notes.append("one of: " + ", ".join(f"'{v}'" for v in enums[c.name]))
        elif (sample_values and c.type in ("text", "character varying")
              and c.name not in FREE_TEXT and t.row_count):
            ident = pgsql.Identifier(c.name)
            cur.execute(pgsql.SQL(
                "SELECT {c} FROM {t} WHERE {c} IS NOT NULL "
                "GROUP BY {c} ORDER BY count(*) DESC, {c} LIMIT %s").format(
                    c=ident, t=pgsql.Identifier(*t.name.split("."))),
                (ENUM_MAX_DISTINCT + 1,))
            vals = [r[0] for r in cur.fetchall()]
            if c.name.endswith("_code") and len(vals) > ENUM_MAX_DISTINCT:
                c.notes.append("e.g. " + ", ".join(f"'{v}'" for v in vals[:CODE_EXAMPLES]))
            elif 0 < len(vals) <= ENUM_MAX_DISTINCT:
                c.notes.append("values " + ", ".join(f"'{v}'" for v in vals))
        if c.name in curated_cols:
            c.notes.append(curated_cols[c.name])


def fk_graph(tables: dict[str, Table]) -> dict[str, set[str]]:
    """Undirected adjacency over foreign keys."""
    g: dict[str, set[str]] = defaultdict(set)
    for t in tables.values():
        for fk in t.foreign_keys:
            g[fk.table].add(fk.ref_table)
            g[fk.ref_table].add(fk.table)
    return g


def shortest_path(graph: dict[str, set[str]], a: str, b: str, max_hops: int,
                  free: frozenset[str] | set[str] = frozenset(),
                  refs: frozenset[tuple[str, str]] | set[tuple[str, str]] = frozenset(),
                  ) -> list[str] | None:
    """Cheapest FK path from a to b within max_hops; None if there is none.

    A path costs one per table it would ADD; tables in `free` (already in
    the prompt) cost nothing. Plain BFS treats every equal-length path alike,
    and the hub tables (ops.plants touches five others) then win ties --
    linking orders to runs "through the plant" rather than through the
    allocation table that actually connects them. Preferring paths through
    tables the question already pulled in is a cheap approximation of the
    minimal join tree, and on this schema it picks the semantic path.

    `refs` holds (child, parent) FK pairs. Passing through a parent from one
    child to another -- order -> plant <- line -- is a "V-turn" through a
    shared dimension: it joins every order to every run at the same plant,
    a fan-out that looks like a join and returns confident nonsense. Each
    V-turn costs as much as an extra table, so a real bridge table
    (run_order_allocations) wins over a shared dimension.
    """
    if a == b:
        return [a]
    # (added tables, hops, path) -- heap order makes the tie-break stable.
    heap = [(0, 0, [a])]
    best: dict[str, tuple[int, int]] = {a: (0, 0)}
    while heap:
        cost, hops, path = heapq.heappop(heap)
        node = path[-1]
        if node == b:
            return path
        if hops >= max_hops:
            continue
        for nxt in sorted(graph.get(node, ())):
            if nxt in path:
                continue
            c = cost + (0 if nxt in free or nxt == b else 1)
            if len(path) > 1 and (path[-2], node) in refs and (nxt, node) in refs:
                c += 1
            if nxt in best and best[nxt] <= (c, hops + 1):
                continue
            best[nxt] = (c, hops + 1)
            heapq.heappush(heap, (c, hops + 1, path + [nxt]))
    return None
