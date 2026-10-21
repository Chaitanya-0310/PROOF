"""CLI:  python -m proof.text2sql "question" [-k 4] [--sql-only] [--route-only] [--json]

--route-only needs no API key: it shows which tables the router would put in
the prompt, which is the first thing to check when an answer is wrong.
"""
from __future__ import annotations

import argparse
import json
import sys

from proof.db import load_env


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(prog="python -m proof.text2sql")
    p.add_argument("question")
    p.add_argument("-k", "--top-k", type=int, default=None,
                   help="tables to retrieve before join-path expansion (default 4)")
    p.add_argument("--route-only", action="store_true",
                   help="show the routed tables and their DDL; no model call")
    p.add_argument("--sql-only", action="store_true",
                   help="generate and run the SQL, skip the written answer")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    from proof.text2sql.router import DEFAULT_TOP_K, route
    top_k = args.top_k or DEFAULT_TOP_K

    if args.route_only:
        r = route(args.question, top_k)
        if args.json:
            print(json.dumps(r.as_dict(), indent=2))
        else:
            _print_routing(r)
            print("\n" + r.schema_text())
        return 0

    from proof.text2sql.pipeline import ask
    res = ask(args.question, top_k=top_k, answer=not args.sql_only)
    if args.json:
        print(json.dumps(res.as_dict(), indent=2, default=str))
        return 0 if res.status == "ok" else 1

    _print_routing(res.routing)
    for i, a in enumerate(res.attempts, 1):
        if a.error:
            print(f"\nattempt {i} failed ({a.error_kind}): {a.error}", file=sys.stderr)
    if res.status != "ok":
        print(f"\n{res.status.upper()}: {res.answer or 'no working query after repairs'}")
        return 1

    print(f"\nSQL:\n{res.sql}")
    if res.assumptions:
        print(f"\nAssumptions: {res.assumptions}")
    print(f"\n{res.result['row_count']} row(s)"
          + ("  (TRUNCATED)" if res.result.get("truncated") else "")
          + f"  · tables read: {', '.join(res.result.get('tables', []))}")
    _print_rows(res.result["rows"])
    if res.answer:
        print(f"\n{res.answer}")
    print(f"\n{res.model} · {res.input_tokens:,} in / {res.output_tokens:,} out"
          f" · ~${res.cost_usd:.4f} · {len(res.attempts)} attempt(s)", file=sys.stderr)
    return 0


def _print_routing(r) -> None:
    print(f"Router: {len(r.tables)} of {r.total_tables} tables")
    for t in r.tables:
        score = f"{t.score:.4f}" if t.score is not None else "     -"
        ranks = (f"lex#{t.lexical_rank or '-'} dense#{t.dense_rank or '-'}"
                 if t.via == "retrieved" else "")
        print(f"  {t.via:9s} {score}  {t.name:30s} {ranks}")


def _print_rows(rows: list[dict], limit: int = 15) -> None:
    if not rows:
        return
    cols = list(rows[0])
    shown = rows[:limit]
    width = {c: min(28, max(len(c), *(len(str(r[c])) for r in shown))) for c in cols}
    print("  " + "  ".join(c[:width[c]].ljust(width[c]) for c in cols))
    for r in shown:
        print("  " + "  ".join(str(r[c])[:width[c]].ljust(width[c]) for c in cols))
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more")


if __name__ == "__main__":
    raise SystemExit(main())
