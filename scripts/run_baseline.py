#!/usr/bin/env python3
"""
Phase 6 entry point: the baseline harness.

    make baseline           # offline, no API key
    make baseline-live      # runs the agent, needs ANTHROPIC_API_KEY

Measures:
  - scrap avoided: WIP dough PROOF surfaces in time to salvage
  - time-to-decision: agent response time vs manual phone-tree delay
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.db import load_env  # noqa: E402

load_env()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 6 baseline harness: time-to-decision, scrap avoided")
    p.add_argument("--live", action="store_true",
                   help="Run the agent on Demo 1 and measure real response time "
                        "(needs ANTHROPIC_API_KEY)")
    p.add_argument("--manual-delay", type=float, default=35.0,
                   help="Manual awareness delay in minutes (default 35)")
    p.add_argument("--agent-estimate", type=float, default=2.0,
                   help="Estimated agent response time in minutes, for offline "
                        "mode (default 2)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from evals.baseline.harness import (
        run_scenario_1_offline,
        run_scenario_1_live,
        run_scenario_2_offline,
        run_scenario_3_offline,
    )
    from evals.baseline.report import (
        print_scenario_1,
        print_scenario_2,
        print_scenario_3,
        run_assertions,
        print_limits,
    )

    print(f"Phase 6 — baseline harness ({'live' if args.live else 'offline'})")
    print(f"  manual delay:    {args.manual_delay:.0f} min")
    if not args.live:
        print(f"  agent estimate:  {args.agent_estimate:.0f} min")

    # --- Scenario 1: line down ---
    if args.live:
        b1 = asyncio.run(run_scenario_1_live(
            manual_minutes=args.manual_delay,
        ))
    else:
        b1 = run_scenario_1_offline(
            agent_minutes=args.agent_estimate,
            manual_minutes=args.manual_delay,
        )
    print_scenario_1(b1)

    # --- Scenario 2: supplier slip ---
    b2 = run_scenario_2_offline(
        agent_minutes=args.agent_estimate,
        manual_minutes=args.manual_delay,
    )
    print_scenario_2(b2)

    # --- Scenario 3: scrap signal ---
    b3 = run_scenario_3_offline(
        agent_minutes=args.agent_estimate,
        manual_minutes=args.manual_delay,
    )
    print_scenario_3(b3)

    # --- Assertions ---
    failures = run_assertions(b1, b2, b3)

    # --- Limits ---
    print_limits()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
