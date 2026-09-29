"""
Router benchmark: does routing make a task faster or cheaper WITHOUT making
the answer wrong?

Runs the same golden-set tasks through the coordinator under each router arm
and compares time, cost and graded quality side by side:

  off    today's system: one fixed model everywhere (the baseline)
  light  always the light tier -- the CONTROL. If Jev beats `off` but not
         `light`, the win came from the cheaper model, not from routing.
  jev    Jev picks light or strong per decision (needs OPENROUTER_API_KEY)
  jev-router
         the coordinator runs on OpenRouter's typesafe/jev-router; sub-agents
         stay fixed, so vs `off` this isolates the coordinator (needs the key)
  openrouter-fixed
         the control for jev-router: same provider, one fixed model, no Jev

Method, and why:

  * Arms are INTERLEAVED and rotated per task. Gateway latency drifts over
    minutes; running all of one arm then all of the next would bake that
    drift into the comparison.
  * The semantic cache is forced OFF. A cache hit would make a later arm
    look fast for a reason that has nothing to do with routing.
  * Every answer is graded with the Phase 5 graders. Speed that costs a
    must-pass grader is not an improvement, and the table puts the two side
    by side so that cannot be missed.
  * Results are written after EVERY run, so a crash or Ctrl-C keeps what
    finished.

Honest limits, printed with the results: a few reps per task is enough to see
a large effect, not to claim a small one. Several golden tasks can queue real
proposals into act.proposed_actions (as `make eval` does); `make reset` gives
a clean floor afterwards.

Run:  make bench-router                       # off vs light, 2 reps
      make bench-router ARMS=off,light,jev REPS=3
      python scripts/bench_router.py --summarize data/bench/<file>.json
      python scripts/bench_router.py --summarize base.json jev.json   # compare runs
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

# Before ANY proof import: the cache reads this once, at import.
os.environ["PROOF_CACHE_ENABLED"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proof.db import load_env  # noqa: E402

load_env()
os.environ["PROOF_CACHE_ENABLED"] = "0"   # .env must not switch it back on

GOLDEN = ROOT / "evals" / "golden.jsonl"
OUT_DIR = ROOT / "data" / "bench"

# A spread of difficulty: the hard multi-domain hero task, a medium
# two-domain task, a citation/RAG task, and a single-domain lookup -- the kind
# of work a router is supposed to send to the light tier.
DEFAULT_CASES = ["line_down_hero", "supplier_slip", "restart_procedure",
                 "single_domain_balance"]


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------
async def run_one(arm: str, case: dict, timeout_s: float) -> dict:
    from evals.graders import resolve as grader
    from evals.run import _record_from_result
    from proof import trace as ptrace
    from proof.agents.config import price_known
    from proof.agents.coordinator import ask
    from proof.identity import resolve as principal_of

    os.environ["PROOF_ROUTER"] = arm
    ptrace.configure(run_label="bench_router")
    row: dict = {"arm": arm, "case": case["case_id"], "ok": False,
                 "started": datetime.now().isoformat(timespec="seconds")}
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            ask(case["question"], principal=principal_of(case["principal"])),
            timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 -- a failed run is a data point
        # MCP/anyio wrap the real failure in ExceptionGroups; "unhandled
        # errors in a TaskGroup" says nothing, so record the innermost one.
        while getattr(exc, "exceptions", None):
            exc = exc.exceptions[0]
        row.update(error=f"{type(exc).__name__}: {exc}",
                   wall_s=round(time.perf_counter() - t0, 3))
        return row

    t = result.timing
    sub_routes = [r.route for r in result.subagent_results if r.route]
    # coordinator_models is what actually served each coordinator turn --
    # under jev-router, Jev's picks rather than the router id.
    models_used = ([result.model] + result.coordinator_models
                   + [r.model for r in result.subagent_results if r.model])
    calls_by_model: dict[str, int] = {}
    for c in t["model"]["per_call"]:
        calls_by_model[c["model"]] = calls_by_model.get(c["model"], 0) + 1

    rec = _record_from_result(case, result)
    grades = [grader(g)(rec) for g in case["graders"]]

    row.update(
        ok=True,
        total_s=t["total_s"],
        answer_s=t.get("time_to_answer_s", t["total_s"]),
        startup_s=t["startup_s"],
        model_wall_s=t["model"]["wall_s"],
        model_sum_s=t["model"]["sum_s"],
        coordinator_model_s=t["model"]["coordinator_s"],
        subagent_model_s=t["model"]["subagent_s"],
        model_calls=t["model"]["calls"],
        median_call_s=t["model"]["median_call_s"],
        tool_wall_s=t["tools"]["wall_s"],
        tool_calls=t["tools"]["calls"],
        router_wall_s=t["router"]["wall_s"],
        router_decisions=t["router"]["decisions"],
        orchestration_s=t["orchestration_s"],
        input_tokens=result.total_input,
        output_tokens=result.total_output,
        cost_usd=round(result.cost_usd, 6),
        # Trustworthy when every call was billed by the provider or priced
        # from a known rate; the budget counts the ones that were neither.
        prices_known=(result.budget.unpriced_calls == 0 if result.budget
                      else all(price_known(m) for m in models_used)),
        coordinator_model=result.model,
        coordinator_route=result.route,
        coordinator_models=result.coordinator_models,
        subagent_routes=[{"domain": r.domain, "model": r.model,
                          "tier": (r.route or {}).get("tier"),
                          "confidence": (r.route or {}).get("confidence"),
                          "reason": (r.route or {}).get("reason")}
                         for r in result.subagent_results],
        calls_by_model=calls_by_model,
        router_cost_usd=round(sum((r or {}).get("cost_usd", 0.0) for r in
                                  [result.route] + sub_routes), 6),
        domains=[r.domain for r in result.subagent_results],
        budget_tripped=rec.budget_tripped,
        grades=[{"grader": g.grader, "passed": g.passed, "severity": g.severity,
                 "reason": g.reason} for g in grades],
        must_pass=all(g.passed for g in grades if g.severity == "must"),
        should_failures=sum(1 for g in grades if not g.passed and g.severity == "should"),
        answer_chars=len(result.answer),
        # False when the coordinator ended with no text at all -- e.g. it
        # spent every iteration on tool calls the SDK rejected. The graders
        # can still pass such a run on routing alone, so it is counted apart.
        answered=result.answer != "(no answer produced)",
    )
    return row


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def _p(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _med(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def summarize(rows: list[dict]) -> str:
    arms = list(dict.fromkeys(r["arm"] for r in rows))
    cases = list(dict.fromkeys(r["case"] for r in rows))
    out: list[str] = []

    def arm_stats(arm: str) -> dict:
        rs = [r for r in rows if r["arm"] == arm]
        ok = [r for r in rs if r["ok"]]
        light = sum(sum(n for m, n in r["calls_by_model"].items()
                        if m == os.getenv("PROOF_ROUTER_LIGHT_MODEL",
                                          "deepseek-v4-flash-0731"))
                    for r in ok)
        calls = sum(r["model_calls"] for r in ok)
        return {
            "runs": len(rs), "ok": len(ok),
            "must_pass": sum(r["must_pass"] for r in ok),
            "answered": sum(r.get("answered", True) for r in ok),
            "answer_p50": _med([r["answer_s"] for r in ok]),
            "answer_p90": _p([r["answer_s"] for r in ok], 0.9),
            "model_p50": _med([r["model_wall_s"] for r in ok]),
            "calls_p50": _med([r["model_calls"] for r in ok]),
            "call_p50": _med([r["median_call_s"] for r in ok]),
            "tools_p50": _med([r["tool_wall_s"] for r in ok]),
            "router_p50": _med([r["router_wall_s"] for r in ok]),
            "cost_mean": statistics.fmean([r["cost_usd"] for r in ok]) if ok else float("nan"),
            "light_share": (light / calls) if calls else 0.0,
            "prices_known": all(r["prices_known"] for r in ok),
        }

    stats = {a: arm_stats(a) for a in arms}

    out.append("PER ARM  (medians across all runs; answer = time until the answer existed)")
    out.append(f"{'arm':10s} {'runs':>4s} {'ok':>3s} {'answered':>8s} {'must-pass':>9s} "
               f"{'answer p50':>10s} {'p90':>7s} {'model p50':>9s} "
               f"{'calls':>5s} {'s/call':>6s} {'tools':>6s} {'router':>6s} "
               f"{'cost/task':>10s} {'light%':>6s}")
    for a in arms:
        s = stats[a]
        mp = f"{s['must_pass']}/{s['ok']}"
        cost = f"${s['cost_mean']:.5f}" + ("" if s["prices_known"] else "*")
        ans = f"{s['answered']}/{s['ok']}"
        out.append(f"{a:10s} {s['runs']:4d} {s['ok']:3d} {ans:>8s} {mp:>9s} "
                   f"{s['answer_p50']:9.1f}s {s['answer_p90']:6.1f}s {s['model_p50']:8.1f}s "
                   f"{s['calls_p50']:5.0f} {s['call_p50']:5.1f}s {s['tools_p50']:5.1f}s "
                   f"{s['router_p50']:5.2f}s {cost:>10s} {100 * s['light_share']:5.0f}%")
    if not all(s["prices_known"] for s in stats.values()):
        out.append("  * a model's price is not in PROOF_MODEL_PRICES; its cost is "
                   "estimated at the default rate and is NOT reliable.")

    if "off" in stats:
        base = stats["off"]
        out.append("\nVS BASELINE (off)")
        for a in arms:
            if a == "off":
                continue
            s = stats[a]

            def d(x: float, y: float) -> str:
                return f"{100 * (x - y) / y:+.0f}%" if y and y == y and x == x else "n/a"

            mp_a = s["must_pass"] / s["ok"] if s["ok"] else 0
            mp_b = base["must_pass"] / base["ok"] if base["ok"] else 0
            out.append(f"  {a:10s} answer time {d(s['answer_p50'], base['answer_p50']):>6s}   "
                       f"model time {d(s['model_p50'], base['model_p50']):>6s}   "
                       f"cost {d(s['cost_mean'], base['cost_mean']):>6s}   "
                       f"must-pass {100 * (mp_a - mp_b):+.0f} pts")

    out.append("\nPER TASK  (answer p50 seconds / must-pass)")
    out.append(f"{'case':24s} " + " ".join(f"{a:>16s}" for a in arms))
    for c in cases:
        cells = []
        for a in arms:
            ok = [r for r in rows if r["arm"] == a and r["case"] == c and r["ok"]]
            n = len([r for r in rows if r["arm"] == a and r["case"] == c])
            if not ok:
                cells.append(f"{'failed x' + str(n):>16s}")
                continue
            cells.append(f"{_med([r['answer_s'] for r in ok]):7.1f}s "
                         f"{sum(r['must_pass'] for r in ok)}/{len(ok)} pass".rjust(16))
        out.append(f"{c:24s} " + " ".join(cells))

    fails = [r for r in rows if r["ok"] and not r["must_pass"]]
    if fails:
        out.append("\nMUST-PASS FAILURES")
        for r in fails:
            for g in r["grades"]:
                if not g["passed"] and g["severity"] == "must":
                    out.append(f"  [{r['arm']}] {r['case']}: {g['grader']} -> {g['reason']}")
    errs = [r for r in rows if not r["ok"]]
    if errs:
        out.append("\nRUN ERRORS")
        for r in errs:
            out.append(f"  [{r['arm']}] {r['case']}: {r['error']}")

    n_min = min((stats[a]["ok"] for a in arms), default=0)
    out.append(f"\nLimits: {n_min} successful run(s) per arm at minimum. Enough to "
               "see a large effect, not to claim a small one.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
async def main_async(args: argparse.Namespace) -> int:
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    jev_arms = [a for a in arms if a in ("jev", "jev-router", "openrouter-fixed")]
    if jev_arms and not os.getenv("OPENROUTER_API_KEY"):
        print(f"The {'/'.join(jev_arms)} arm needs OPENROUTER_API_KEY. Without it the router "
              "fail-safes every decision to the strong tier, which would just "
              "re-run the baseline and report it as Jev. Refusing.",
              file=sys.stderr)
        return 2

    wanted = [c.strip() for c in args.cases.split(",") if c.strip()]
    golden = {c["case_id"]: c for c in
              (json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines()
               if l.strip())}
    missing = [c for c in wanted if c not in golden]
    if missing:
        print(f"unknown case(s): {missing}", file=sys.stderr)
        return 2
    cases = [golden[c] for c in wanted]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else (
        OUT_DIR / f"router_{datetime.now():%Y%m%d_%H%M%S}.json")

    plan = []
    for rep in range(args.reps):
        for ci, case in enumerate(cases):
            k = (rep + ci) % len(arms)
            for arm in arms[k:] + arms[:k]:
                plan.append((rep, arm, case))

    print(f"{len(plan)} runs: arms={arms} cases={wanted} reps={args.reps}")
    shown = out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path
    print(f"writing {shown} after every run\n")
    rows: list[dict] = []
    for i, (rep, arm, case) in enumerate(plan, 1):
        print(f"[{i:3d}/{len(plan)}] rep {rep + 1} {arm:5s} {case['case_id']:24s} ",
              end="", flush=True)
        row = await run_one(arm, case, args.timeout)
        row["rep"] = rep + 1
        rows.append(row)
        if row["ok"]:
            print(f"{row['answer_s']:6.1f}s  {row['model_calls']:2d} calls  "
                  f"{'PASS' if row['must_pass'] else 'FAIL'}  ${row['cost_usd']:.5f}", flush=True)
        else:
            print(f"ERROR {row['error'][:90]}", flush=True)
        out_path.write_text(json.dumps({"arms": arms, "cases": wanted,
                                        "reps": args.reps, "rows": rows},
                                       indent=2), encoding="utf-8")

    print("\n" + summarize(rows))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--arms", default="off,light")
    p.add_argument("--cases", default=",".join(DEFAULT_CASES))
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--timeout", type=float, default=600.0,
                   help="per-run ceiling in seconds")
    p.add_argument("--out", default=None)
    p.add_argument("--summarize", metavar="JSON", nargs="+",
                   help="re-print the summary of saved run(s), merged -- e.g. a "
                        "baseline file and a jev-router file; runs nothing")
    args = p.parse_args()
    if args.summarize:
        rows = [r for f in args.summarize
                for r in json.loads(Path(f).read_text(encoding="utf-8"))["rows"]]
        print(summarize(rows))
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
