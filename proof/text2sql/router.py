"""The table router: which few tables does this question need?

Two stages:

1. **Hybrid retrieval** over meta.schema_index -- Postgres full-text plus
   pgvector, fused with weighted RRF. Same recipe and same lexical weight as
   the SOP retriever (proof/rag/retrieve.py) and for the same reason: plant
   vocabulary ('TOR1', 'MATERIAL_OUT', 'scrap') is where lexical search is
   strong and a general-purpose embedding is weak, while "which customers get
   hurt" has no word in common with `run_order_allocations` and only the
   dense arm finds it.

2. **Join-path expansion.** Retrieval scores tables one at a time, so it
   will happily return `scm.customers` and `ops.production_runs` for "which
   customers does run 923 supply" and miss the three tables in between. A
   model given only the endpoints invents a join -- usually on a column that
   happens to share a name -- and the number it returns looks fine. So for
   every pair of retrieved tables the shortest foreign-key path between them
   is added, up to MAX_BRIDGE_HOPS. That is what turns top-k *tables* into a
   schema slice a correct query can actually be written against.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from proof.rag.retrieve import LEX_WEIGHT, RRF_K, embed_query
from proof.tools._base import query

from .catalog import shortest_path

DEFAULT_TOP_K = 4
# Paths longer than this are not bridged: a 5-hop join between two retrieved
# tables usually means one of them was a weak hit, and pulling in the whole
# path would spend the context budget the router exists to save.
MAX_BRIDGE_HOPS = 4
MAX_TABLES = 9


@dataclass
class RoutedTable:
    name: str
    ddl: str
    neighbours: list[str]
    via: str                        # retrieved | bridge | pinned | requested
    score: float | None = None
    lexical_rank: int | None = None
    dense_rank: int | None = None


@dataclass
class Routing:
    question: str
    tables: list[RoutedTable] = field(default_factory=list)
    total_tables: int = 0

    @property
    def names(self) -> list[str]:
        return [t.name for t in self.tables]

    def schema_text(self) -> str:
        """The DDL slice that goes into the prompt -- and nothing else."""
        return "\n\n".join(t.ddl for t in self.tables)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "tables_in_index": self.total_tables,
            "tables_selected": len(self.tables),
            "tables": [{"table": t.name, "via": t.via, "score": t.score,
                        "lexical_rank": t.lexical_rank,
                        "dense_rank": t.dense_rank} for t in self.tables],
        }


def route(question: str, top_k: int = DEFAULT_TOP_K, *,
          bridge: bool = True, mode: str = "hybrid",
          extra: list[str] | None = None) -> Routing:
    """Select the tables a question needs, with the DDL to prompt on.

    `extra` forces tables in (the repair loop uses it when a generated query
    names a real table the router missed). `mode` exists for the eval in
    scripts/smoke_text2sql.py and, as with SOP retrieval, is not exposed to
    the agent.
    """
    if mode not in ("hybrid", "lexical", "dense"):
        raise ValueError(f"mode must be hybrid, lexical or dense; got {mode!r}")
    index = _load_index()
    hits = _search(question, top_k, mode)

    chosen: dict[str, RoutedTable] = {}
    for h in hits:
        row = index[h["table_name"]]
        chosen[h["table_name"]] = RoutedTable(
            h["table_name"], row["ddl"], row["neighbours"], "retrieved",
            float(h["rrf_score"]), h["lexical_rank"], h["dense_rank"])
    for name in extra or []:
        if name in index and name not in chosen:
            row = index[name]
            chosen[name] = RoutedTable(name, row["ddl"], row["neighbours"], "requested")

    if bridge:
        graph = {name: set(row["neighbours"]) for name, row in index.items()}
        refs = {(e["from"], e["to"]) for row in index.values() for e in row["fk_edges"]}
        for a, b in combinations(list(chosen), 2):
            path = shortest_path(graph, a, b, MAX_BRIDGE_HOPS,
                                 free=set(chosen), refs=refs)
            for name in (path or [])[1:-1]:
                if name not in chosen and len(chosen) < MAX_TABLES:
                    row = index[name]
                    chosen[name] = RoutedTable(name, row["ddl"], row["neighbours"], "bridge")

    for name, row in index.items():
        if row["pinned"] and name not in chosen:
            chosen[name] = RoutedTable(name, row["ddl"], row["neighbours"], "pinned")

    return Routing(question, list(chosen.values()), total_tables=len(index))


def describe(tables: list[str]) -> dict[str, Any]:
    """Enriched DDL for named tables -- the escape hatch for a router miss."""
    index = _load_index()
    found = [n for n in tables if n in index]
    missing = [n for n in tables if n not in index]
    out: dict[str, Any] = {
        "rows": [{"table": n, "ddl": index[n]["ddl"]} for n in found],
        "row_count": len(found),
    }
    if missing:
        out["note"] = (f"Not in the queryable schema: {', '.join(missing)}. "
                       f"Available: {', '.join(sorted(index))}")
    return out


def all_tables() -> list[str]:
    return sorted(_load_index())


def _load_index() -> dict[str, dict[str, Any]]:
    # ~20 rows of text, read per question. Not cached: a re-index should be
    # visible to a long-lived MCP server without a restart.
    res = query("SELECT table_name, ddl, neighbours, fk_edges, pinned FROM meta.schema_index")
    if not res["rows"]:
        raise RuntimeError("meta.schema_index is empty -- run `make sql-index` first")
    return {r["table_name"]: r for r in res["rows"]}


def _search(question: str, top_k: int, mode: str) -> list[dict[str, Any]]:
    w = LEX_WEIGHT
    return query("""
        WITH q AS (
            -- OR semantics, as in proof/rag/retrieve.py: the AND that
            -- websearch_to_tsquery builds would demand every word of the
            -- question appear in one table's text and match nothing.
            SELECT replace(websearch_to_tsquery('english', %(q)s)::text, '&', '|')::tsquery AS tsq
        ),
        lexical AS (
            SELECT s.table_name,
                   row_number() OVER (ORDER BY ts_rank_cd(s.tsv, q.tsq) DESC, s.table_name) AS rank
            FROM meta.schema_index s, q
            WHERE s.tsv @@ q.tsq AND NOT s.pinned
            LIMIT 20
        ),
        dense AS (
            SELECT table_name,
                   row_number() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rank
            FROM meta.schema_index
            WHERE NOT pinned
            ORDER BY embedding <=> %(qvec)s::vector
            LIMIT 20
        ),
        fused AS (
            SELECT COALESCE(l.table_name, d.table_name) AS table_name,
                   COALESCE(CASE WHEN %(use_lex)s THEN %(w_lex)s / (%(k)s + l.rank) END, 0)
                   + COALESCE(CASE WHEN %(use_den)s THEN %(w_den)s / (%(k)s + d.rank) END, 0)
                       AS rrf_score,
                   l.rank AS lexical_rank, d.rank AS dense_rank
            FROM lexical l FULL OUTER JOIN dense d ON d.table_name = l.table_name
        )
        SELECT table_name, round(rrf_score::numeric, 5) AS rrf_score,
               lexical_rank, dense_rank
        FROM fused
        WHERE rrf_score > 0
        ORDER BY rrf_score DESC, table_name
        LIMIT %(top_k)s
    """, {"q": question, "qvec": embed_query(question), "k": RRF_K,
          "top_k": top_k,
          "use_lex": mode in ("hybrid", "lexical"),
          "use_den": mode in ("hybrid", "dense"),
          "w_lex": 1.0 if mode == "lexical" else w,
          "w_den": 1.0 if mode == "dense" else 1.0 - w})["rows"]

