"""Offline step: introspect the schema, enrich it, embed it, store it.

Run:  make sql-index   (after `make sql-migrate` on an existing database)

Re-run whenever the schema changes or descriptions.py is edited. It is cheap
-- one embedding per table -- and it is the ONLY place the schema the model
sees is decided, so a stale index is the first thing to rule out when a
generated query names a column that no longer exists.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from proof.db import connect  # noqa: E402
from proof.rag.index import EMBED_DIM, embed  # noqa: E402
from proof.text2sql.catalog import PINNED, load_catalog  # noqa: E402
from proof.text2sql.descriptions import TABLES as CURATED  # noqa: E402


def main() -> None:
    # Introspect as agent_read: the index should describe exactly what the
    # query path can read, no more.
    with connect("agent_read") as conn:
        tables = load_catalog(conn)
    print(f"Indexing {len(tables)} tables for text-to-SQL")

    undocumented = sorted(set(tables) - set(CURATED))
    if undocumented:
        print(f"  no curated description (DDL only): {', '.join(undocumented)}")
    stale = sorted(set(CURATED) - set(tables))
    if stale:
        print(f"  descriptions.py names tables that do not exist: {', '.join(stale)}")

    ordered = sorted(tables.values(), key=lambda t: t.name)
    vectors = embed([f"{t.name}\n{t.search_text()}" for t in ordered])
    if len(vectors[0]) != EMBED_DIM:
        raise SystemExit(f"model returned dim {len(vectors[0])}, expected {EMBED_DIM}")

    with connect("seeder") as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE meta.schema_index")
            cur.executemany("""
                INSERT INTO meta.schema_index (table_name, schema_name,
                    description, ddl, search_text, neighbours, fk_edges,
                    row_count, pinned, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, [
                (t.name, t.schema, t.description, t.ddl(), t.search_text(),
                 t.neighbours, json.dumps([fk.as_dict() for fk in t.foreign_keys]),
                 t.row_count, t.name in PINNED,
                 "[" + ",".join(f"{v:.6f}" for v in vec) + "]")
                for t, vec in zip(ordered, vectors)
            ])
        conn.commit()

    widest = max(ordered, key=lambda t: len(t.ddl()))
    total = sum(len(t.ddl()) for t in ordered) // 4
    print(f"  indexed. full schema ~{total:,} tokens of DDL; "
          f"largest table {widest.name} ~{len(widest.ddl()) // 4} tokens")


if __name__ == "__main__":
    main()
