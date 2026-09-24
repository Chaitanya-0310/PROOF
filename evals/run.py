"""
The eval runner.

Two modes, and the split is the point:

  --check-graders   Offline. Runs every grader against the hand-written
                    fixtures and checks each verdict matches what the fixture
                    expects. Needs no API key and no model. This is what
                    proves the graders DISCRIMINATE -- that both_losses fails
                    the regression fixture and passes the good one -- so the
                    eval is trustworthy before it is ever pointed at a model.

  (default)         Live. Drives the coordinator over the golden set, builds a
                    RunRecord per case, grades it. Needs ANTHROPIC_API_KEY,
                    because this is the part that actually exercises the agent.

An eval you can't check is an eval you can't trust. `--check-graders` is the
answer to "how do you know the eval itself is right", and it runs in CI.

Run:  make eval-check      (offline, grader self-test)
      make eval            (live, needs a key)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.graders import resolve  # noqa: E402
from evals.schema import DomainRecord, GradeResult, RunRecord  # noqa: E402

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden.jsonl"
FIXTURES = HERE / "fixtures" / "cases.json"


# =====================================================================
# Offline: check the graders against fixtures
# =====================================================================
def check_graders() -> int:
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fixtures = data["fixtures"]
    mismatches = 0
    total = 0

    print(f"grader self-check: {len(fixtures)} fixtures\n")
    for fx in fixtures:
        rec = RunRecord.from_dict({**fx["record"],
                                   "question": fx["record"].get("question", ""),
                                   "answer": fx["record"].get("answer", "")})
        line = [f"  {fx['fixture_id']:26s}"]
        ok_all = True
        for grader_name, expected in fx["expect"].items():
            total += 1
            got = resolve(grader_name)(rec).passed
            mark = "ok" if got == expected else "MISMATCH"
            if got != expected:
                mismatches += 1
                ok_all = False
            line.append(f"{grader_name}={'P' if got else 'F'}"
                        + ("" if got == expected else f"(want {'P' if expected else 'F'})"))
        print(("  [PASS] " if ok_all else "  [FAIL] ") + fx["fixture_id"])
        for grader_name, expected in fx["expect"].items():
            got = resolve(grader_name)(rec)
            flag = "ok " if got.passed == expected else "!! "
            print(f"        {flag}{grader_name:34s} -> "
                  f"{'pass' if got.passed else 'fail'}: {got.reason}")

    print(f"\n{total - mismatches}/{total} grader verdicts matched expectations")
    if mismatches:
        print("A mismatch means a grader accepts something it should reject "
              "(or vice versa) -- fix the grader, not the fixture.")
    return 1 if mismatches else 0


# =====================================================================
# Live: run the golden set through the coordinator
# =====================================================================
def _record_from_result(case: dict, result) -> RunRecord:
    """Flatten a CoordinatorResult into the snapshot graders read."""
    domains = []
    for r in result.subagent_results:
        domains.append(DomainRecord(
            domain=r.domain,
            tools_called=[c["tool"] for c in r.tool_calls],
            citation_status=r.citation_status,
            authorization=getattr(r, "authorization", "allowed"),
        ))
    b = result.budget
    return RunRecord(
        case_id=case["case_id"], question=case["question"],
        principal=case["principal"], answer=result.answer,
        domains=domains,
        model_calls=(b.model_calls if b else 0),
        cost_usd=(b.cost_usd if b else 0.0),
        budget_tripped=(b.tripped if b else None),
    )


async def run_live(only: str | None) -> int:
    from proof import trace as ptrace
    from proof.agents.coordinator import ask
    from proof.identity import resolve as resolve_principal

    cases = [json.loads(l) for l in
             GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    if only:
        cases = [c for c in cases if c["case_id"] == only]
        if not cases:
            print(f"no case {only!r} in the golden set", file=sys.stderr)
            return 1

    must_fail = should_fail = 0
    print(f"running {len(cases)} golden case(s) through the coordinator\n")
    for case in cases:
        ptrace.configure(run_label=f"eval_{case['case_id']}")
        principal = resolve_principal(case["principal"])
        result = await ask(case["question"], principal=principal)
        rec = _record_from_result(case, result)

        grades = [resolve(g)(rec) for g in case["graders"]]
        case_must = [g for g in grades if not g.passed and g.severity == "must"]
        case_should = [g for g in grades if not g.passed and g.severity == "should"]
        must_fail += len(case_must)
        should_fail += len(case_should)

        status = "PASS" if not case_must else "FAIL"
        print(f"  [{status}] {case['case_id']:24s} "
              f"{rec.model_calls} calls, ${rec.cost_usd:.3f}")
        for g in grades:
            if not g.passed:
                print(f"        {g.severity}: {g.grader} -> {g.reason}")

    print(f"\n{'=' * 68}")
    print(f"must-pass failures: {must_fail}   should failures: {should_fail}")
    return 1 if must_fail else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="proof-eval", description=__doc__)
    p.add_argument("--check-graders", action="store_true",
                   help="offline: test the graders against fixtures (no key)")
    p.add_argument("--case", default=None, help="run one golden case by id")
    args = p.parse_args()

    if args.check_graders:
        return check_graders()

    from proof.db import load_env
    load_env()
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        print("Live eval needs ANTHROPIC_API_KEY (add it to .env).\n"
              "The graders themselves are testable without a key:\n"
              "  make eval-check", file=sys.stderr)
        return 1
    return asyncio.run(run_live(args.case))


if __name__ == "__main__":
    raise SystemExit(main())
