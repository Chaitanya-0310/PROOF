"""PROOF command line.

    make ask Q="Line 3 is down, what's at risk?"
    make demo1

Prints the answer, then the delegation trace and an estimated cost. The trace
is not debug output -- it is the product. A plant manager who cannot see which
systems were consulted has been handed an oracle, and operations people are
right not to trust oracles.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.agents.coordinator import ask  # noqa: E402
from proof.identity import DEMO_PRINCIPALS, resolve  # noqa: E402
from proof.db import connect, load_env, sim_now  # noqa: E402

# Load .env before ANYTHING reads configuration -- including the Anthropic
# SDK, which resolves its key at client construction. Doing this at import
# time rather than inside preflight() means the key is present no matter
# which entry point is used.
load_env()

# The three scripted demos. Worded the way a supervisor would actually say
# it -- terse, with an ETA and no identifiers -- because a question phrased in
# schema terms proves nothing about whether the system is usable.
DEMOS = {
    "demo1": "Line 3 at TOR1 just went down, sheeter failure, maintenance says "
             "90 minutes. What's at risk and what should I do?",
    "demo2": "The flour delivery to TOR1 is running late. What does that break, "
             "and what should we prioritise?",
    "demo3": "Scrap is up on our sweet goods this month. Which line, and why?",
}


def preflight() -> str | None:
    """Fail early and specifically, rather than deep inside an agent loop."""
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return ("ANTHROPIC_API_KEY is not set.\n"
                "  Add it to .env (it is gitignored) or export it.\n"
                "  The tool and MCP layers need no key: try `make smoke` "
                "and `make smoke-mcp`.")
    try:
        with connect("agent_read") as conn:
            sim_now(conn)
    except Exception as exc:  # noqa: BLE001
        return (f"Cannot reach the plant database ({type(exc).__name__}).\n"
                f"  Try: make up && make seed && make scenario\n  {exc}")
    return None


async def run(question: str, verbose: bool) -> int:
    with connect("agent_read") as conn:
        now = sim_now(conn)
    print(f"sim now: {now.isoformat()}")
    print(f"question: {question}\n")

    started = time.monotonic()
    result = await ask(question, verbose=verbose)
    elapsed = time.monotonic() - started

    print("=" * 72)
    print(result.answer)
    print("=" * 72)

    print(f"\ndelegation trace ({len(result.subagent_results)} sub-agent call(s))")
    for r in result.subagent_results:
        print(f"  {r.domain:11s} {len(r.tool_calls)} tool call(s): "
              + ", ".join(c["tool"] for c in r.tool_calls))

    print(f"\n{result.total_input:,} in / {result.total_output:,} out tokens "
          f"~ ${result.cost_usd:.3f}   {elapsed:.1f}s")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="proof", description=__doc__)
    p.add_argument("question", nargs="*", help="operations question, or a demo name")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print each delegation as it happens")
    # Stands in for logging in. In a deployment this comes from the session's
    # OIDC token; the flag exists so the authorization demo can switch between
    # people with genuinely different authority.
    p.add_argument("--as", dest="as_principal", default=None,
                   choices=sorted(DEMO_PRINCIPALS),
                   help="who is asking (default: the shift supervisor)")
    args = p.parse_args()

    raw = " ".join(args.question).strip()
    if not raw:
        print("usage: proof <question|demo1|demo2|demo3>")
        for k, v in DEMOS.items():
            print(f"  {k}: {v}")
        return 2

    question = DEMOS.get(raw, raw)
    if raw in DEMOS:
        print(f"[{raw}]")

    problem = preflight()
    if problem:
        print(problem, file=sys.stderr)
        return 1

    return asyncio.run(run(question, args.verbose, args.as_principal))


if __name__ == "__main__":
    raise SystemExit(main())
