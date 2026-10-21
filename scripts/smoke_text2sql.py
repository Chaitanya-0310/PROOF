"""
Text-to-SQL: router eval + guard tests. No API key needed.

Three jobs:

1. **Measure the router.** A labelled set of questions with the tables a
   correct query must read, scored as table recall and full-coverage rate
   (every needed table in the prompt), for lexical, dense and hybrid, and for
   hybrid with and without join-path bridging. A question whose tables are
   not all in the prompt cannot be answered correctly however good the
   model is -- this is the ceiling on the whole feature, so it is measured
   rather than assumed.

2. **Prove the guard refuses** what it should, including the cases a naive
   keyword filter misses: a write hidden in a CTE, a second statement behind
   a comment, SQL smuggled through query_to_xml, a read outside the plant
   schemas that only EXPLAIN can see.

3. **Prove the role is the backstop.** Bypass the static check and send a
   write straight down the guard's connection: Postgres must refuse it.

Questions are phrased the way people ask, not in table vocabulary.

Run:  make smoke-sql
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from proof.db import load_env  # noqa: E402

load_env()

from proof.text2sql import guard, router  # noqa: E402

# (question, tables a correct query must read). The clock is pinned, so it
# is never a label.
GOLD: list[tuple[str, set[str]]] = [
    ("which lines are down right now and why",
     {"ops.downtime_events", "ops.lines"}),
    ("total downtime minutes by plant over the last 7 days",
     {"ops.downtime_events", "ops.lines", "ops.plants"}),
    ("what are the most common mechanical stoppage reasons at DAL1",
     {"ops.downtime_events", "ops.lines", "ops.plants"}),
    ("scrap rate by sku category this month",
     {"ops.scrap_events", "ops.production_runs", "ops.skus"}),
    ("how much over-proofed dough did we throw away last week",
     {"ops.scrap_events"}),
    ("which customers are supplied by run 923",
     {"scm.run_order_allocations", "scm.customer_order_lines",
      "scm.customer_orders", "scm.customers"}),
    ("open orders for strategic customers shipping in the next 2 days",
     {"scm.customer_orders", "scm.customers"}),
    ("which purchase orders are arriving later than promised",
     {"scm.purchase_orders"}),
    ("how much flour do we have in stock at TOR1",
     {"scm.inventory_lots", "scm.materials", "ops.plants"}),
    ("which supplier has the longest lead time",
     {"scm.suppliers"}),
    ("how many kg of butter does one unit of each croissant sku use",
     {"scm.sku_bom", "scm.materials", "ops.skus"}),
    ("which lines can make FLA-002 and how long is the changeover",
     {"ops.line_sku_compat", "ops.lines", "ops.skus"}),
    ("staged dough batches that expire in the next hour",
     {"ops.wip_batches"}),
    ("products that contain sesame",
     {"ops.skus"}),
    ("open quality holds by plant",
     {"qms.quality_holds", "ops.plants"}),
    ("how many proposals did each plant manager approve",
     {"act.proposed_actions", "act.principals"}),
    ("who was refused permission to do something and why",
     {"act.authz_denials"}),
    ("planned versus actual units for runs completed yesterday",
     {"ops.production_runs"}),
]

# Recall floors. Set from the first measured run and held there: a change to
# descriptions.py or the fusion weight that drops below them is a regression.
MIN_RECALL_BRIDGED = 0.90
MIN_COVERAGE_BRIDGED = 0.75

failures: list[str] = []


def ok(cond: bool, label: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def eval_router() -> None:
    print("\n1. Router: is every table the query needs in the prompt?")
    print(f"   {len(GOLD)} labelled questions, top_k={router.DEFAULT_TOP_K}\n")
    print(f"   {'arm':22s} {'recall':>7s} {'covered':>8s} {'avg tables':>11s}")
    results = {}
    for label, mode, bridge in (("lexical", "lexical", False),
                                ("dense", "dense", False),
                                ("hybrid", "hybrid", False),
                                ("hybrid + bridges", "hybrid", True)):
        hit = need = covered = n_tables = 0
        misses = []
        for q, gold in GOLD:
            r = router.route(q, mode=mode, bridge=bridge)
            got = set(r.names)
            hit += len(gold & got)
            need += len(gold)
            covered += gold <= got
            n_tables += len(got)
            if not gold <= got:
                misses.append((q, sorted(gold - got)))
        recall, cov = hit / need, covered / len(GOLD)
        results[label] = (recall, cov, misses)
        print(f"   {label:22s} {recall:7.1%} {cov:8.1%} {n_tables / len(GOLD):11.1f}")

    recall, cov, misses = results["hybrid + bridges"]
    if misses:
        print("\n   still missing with bridges:")
        for q, m in misses:
            print(f"     - {q!r}: {', '.join(m)}")
    total = router.route("x").total_tables
    print()
    ok(recall >= MIN_RECALL_BRIDGED, f"hybrid+bridges table recall {recall:.1%} >= {MIN_RECALL_BRIDGED:.0%}")
    ok(cov >= MIN_COVERAGE_BRIDGED, f"hybrid+bridges full coverage {cov:.1%} >= {MIN_COVERAGE_BRIDGED:.0%}")
    ok(results["hybrid + bridges"][1] >= results["hybrid"][1],
       "bridging never lowers coverage")

    r = router.route("which customers are supplied by run 923")
    ok(len(r.tables) < total, f"prompt carries {len(r.tables)} of {total} tables, not all")
    ok("ops.sim_clock" in r.names, "simulation clock is pinned into every prompt")


def test_bridges() -> None:
    print("\n2. Join-path expansion")
    # Retrieval switched off (top_k=0) so this tests the path search alone:
    # the endpoints only, and nothing in between to make the path cheap.
    r = router.route("which customers does a run supply", top_k=0,
                     extra=["scm.customers", "ops.production_runs"])
    names = set(r.names)
    print(f"   customers + production_runs -> {sorted(names)}")
    ok({"scm.customer_orders", "scm.customer_order_lines",
        "scm.run_order_allocations"} <= names,
       "customers <-> runs bridged through orders, lines and allocations")
    ok("ops.plants" not in names,
       "no V-turn through a shared dimension (orders -> plant <- lines)")
    vias = {t.name: t.via for t in r.tables}
    ok(vias.get("scm.run_order_allocations") == "bridge", "bridge tables are labelled as such")


REFUSE = [
    ("INSERT INTO act.proposed_actions DEFAULT VALUES", "plain write"),
    ("SELECT 1; DROP TABLE ops.lines", "second statement"),
    ("SELECT 1 /* ; */ ; DELETE FROM ops.lines", "statement hidden after a comment"),
    ("WITH d AS (DELETE FROM ops.lines RETURNING *) SELECT * FROM d", "write inside a CTE"),
    ("SELECT * INTO scratch FROM ops.lines", "SELECT INTO creates a table"),
    ("SELECT pg_sleep(30)", "sleep"),
    ("SELECT query_to_xml('select * from docs.chunks', true, true, '')", "SQL smuggled in a string"),
    ("SELECT pg_read_file('/etc/passwd')", "file read"),
    ("SELECT set_config('statement_timeout', '0', false)", "settings change"),
    ("SELECT * FROM docs.chunks", "schema outside the allow-list (EXPLAIN)"),
    ("SELECT * FROM meta.schema_index", "the router's own index"),
    ("SELECT usename FROM pg_catalog.pg_user", "system catalogue"),
    ("SELECT * FROM ops.lines FOR UPDATE", "row locks"),
    ("EXPLAIN ANALYZE DELETE FROM ops.lines", "does not start with SELECT"),
]

ALLOW = [
    ("SELECT plant_code FROM ops.plants ORDER BY 1", "simple select"),
    ("select 'delete; drop' AS s, l.line_code -- update\nfrom ops.lines l limit 1",
     "keywords inside strings and comments are not executed"),
    ("WITH r AS (SELECT line_id, count(*) n FROM ops.downtime_events GROUP BY 1) "
     "SELECT l.line_code, r.n FROM r JOIN ops.lines l USING (line_id) ORDER BY 2 DESC",
     "CTE + join"),
    ("SELECT order_code FROM scm.customer_orders WHERE order_code LIKE 'SO-1%' LIMIT 3",
     "LIKE with % and no params"),
]


def test_guard() -> None:
    print("\n3. Guard: refused before execution")
    for sql, why in REFUSE:
        out = guard.run(sql)
        ok("error" in out and out.get("error_kind") in ("refused", "database"),
           f"{why}: {out.get('error', 'EXECUTED')[:70]}")

    print("\n4. Guard: legitimate reads run")
    for sql, why in ALLOW:
        out = guard.run(sql)
        ok("error" not in out and out["row_count"] > 0,
           f"{why}: {out.get('error') or str(out['row_count']) + ' row(s)'}")

    out = guard.run("SELECT s.plant_code FROM ops.plants s")
    ok(out.get("tables") == ["ops.plants"], f"tables read come from the plan: {out.get('tables')}")

    out = guard.run("SELECT * FROM ops.scrap_events")
    ok(out["truncated"] and out["row_count"] == guard.MAX_ROWS and "NOT a total" in out["note"],
       f"large result capped at {guard.MAX_ROWS} and flagged as not a total")

    out = guard.run("SELECT no_such_column FROM ops.lines")
    ok(out.get("error_kind") == "database" and "no_such_column" in out["error"],
       f"database errors come back as data for repair: {out.get('error', '')[:60]}")
    out = guard.run("SELECT 1 AS ok")
    ok("error" not in out, "connection usable after a failed query")


def test_backstop() -> None:
    print("\n5. Backstop: the database refuses writes even with the check bypassed")
    conn = guard._get_conn()
    for sql in ("DELETE FROM ops.lines WHERE false",
                "INSERT INTO act.decision_log (action_id, at, principal_id, event) "
                "SELECT 1, now(), 'x', 'proposed' WHERE false"):
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            refused = False
        except (psycopg.errors.ReadOnlySqlTransaction,
                psycopg.errors.InsufficientPrivilege) as exc:
            refused, why = True, type(exc).__name__
        conn.rollback()
        ok(refused, f"{sql.split()[0]} refused by Postgres ({why if refused else 'ALLOWED'})")


def main() -> int:
    eval_router()
    test_bridges()
    test_guard()
    test_backstop()
    print(f"\n{'=' * 60}")
    print("all passed" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
