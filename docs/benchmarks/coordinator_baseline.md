# Coordinator baseline (router off) — before Jev Router

Recorded 2026-09-28, 18:18–18:34 local. Raw rows:
[`data/bench/baseline_coordinator_off.json`](../../data/bench/baseline_coordinator_off.json).

This is the reference point for replacing the coordinator's fixed model with
OpenRouter's `typesafe/jev-router` (`PROOF_ROUTER=jev-router`). The run that
follows must use the same cases, reps and grader set so the two are comparable.

## Setup

| | |
|---|---|
| Coordinator model | `gpt-5.6-luna` (fixed, `PROOF_ROUTER=off`) |
| Sub-agent model | `gpt-5.6-luna` (unchanged in the Jev run) |
| Endpoint | `ANTHROPIC_BASE_URL=https://api.cheaperinference.com` |
| Pricing used | $0.046 / $0.092 per MTok in / out |
| Cases | `line_down_hero`, `supplier_slip`, `restart_procedure`, `single_domain_balance` |
| Reps | 3 per case, 12 runs total |
| Semantic cache | forced off |
| Grading | Phase 5 golden-set graders (`evals/graders.py`) |

Command:

```bash
uv run python scripts/bench_router.py --arms off --reps 3 \
  --out data/bench/baseline_coordinator_off.json
```

## Headline

| Metric | Baseline |
|---|---|
| Runs completed without error | 12 / 12 |
| **Produced an answer** | **6 / 12** |
| Passed every must-pass grader | 7 / 12 |
| Answered **and** passed | **6 / 12** |
| Answer time, median (p90) | 67.2 s (140.6 s) |
| Coordinator model time, median | 39.2 s |
| Sub-agent model time, median | 27.5 s |
| Model calls per question, median | 13 |
| Agents consulted per question, median | 1.5 |
| Cost per question, mean | $0.00127 |
| MCP startup, median | 7.6 s |
| Budget ceiling tripped | 0 |

The six runs that **did** answer: median 76.4 s, 14 model calls, 3 agents
consulted, $0.00156 per question.

## Per task

| Case | Answered | Must-pass | Answer time p50 |
|---|---|---|---|
| `single_domain_balance` | 3 / 3 | 3 / 3 | 24.1 s |
| `supplier_slip` | 2 / 3 | 2 / 3 | 115.8 s |
| `line_down_hero` | 1 / 3 | 1 / 3 | 72.8 s |
| `restart_procedure` | 0 / 3 | 1 / 3 | 127.2 s |

## Per run

| Rep | Case | Answered | Must-pass | Time | Model calls | Agents consulted |
|---|---|---|---|---|---|---|
| 1 | line_down_hero | yes | pass | 92.5 s | 15 | production, inventory, demand, actions |
| 1 | supplier_slip | yes | pass | 140.6 s | 19 | inventory, production, demand ×2, production, quality, actions |
| 1 | restart_procedure | **no** | pass | 127.2 s | 22 | production, inventory, quality |
| 1 | single_domain_balance | yes | pass | 24.1 s | 4 | production |
| 2 | line_down_hero | **no** | fail | 56.2 s | 10 | — |
| 2 | supplier_slip | yes | pass | 115.8 s | 16 | inventory, production, demand, inventory, actions |
| 2 | restart_procedure | **no** | fail | 61.7 s | 13 | production |
| 2 | single_domain_balance | yes | pass | 22.4 s | 4 | production |
| 3 | line_down_hero | **no** | fail | 72.8 s | 10 | — |
| 3 | supplier_slip | **no** | fail | 55.8 s | 10 | — |
| 3 | restart_procedure | **no** | fail | 142.7 s | 26 | production, inventory |
| 3 | single_domain_balance | yes | pass | 60.3 s | 13 | production ×2 |

## Why half the runs produced nothing

In the three runs with 10 model calls and no agents consulted, the
coordinator used all 10 of its iterations calling tools **with empty
arguments**:

```
ask_production_agent({})  ask_inventory_agent({})  ask_demand_agent({})  ...
→ ValueError('Invalid arguments for function ask_production_agent')
```

The model repeats the same empty calls each round, reaches `max_iterations=10`,
and ends with `(no answer produced)`. Sub-agents show the same symptom in the
MCP server logs (`Tool 'get_staged_wip' rejected arguments: ['line_id']`),
which explains the long, partly failed delegations in the other no-answer runs.

A standalone check against the same gateway, with no PROOF code involved,
returned tool arguments for a **single simple tool only 1 time in 3**. So the
main failure in this baseline is **tool-argument reliability of
`gpt-5.6-luna` through this gateway**, not the coordinator's planning.

Also note: `restart_procedure` rep 1 passed every must-pass grader but
returned no answer. The graders check routing and citations, not whether an
answer exists, which is why the benchmark now reports `answered` separately.

## Reading the Jev Router result against this

- **Primary metric: answered and passed** (baseline 6/12). Time and cost
  only matter among runs that produced a correct answer.
- **A Jev win may come from the provider, not from routing.** `jev-router`
  sends the coordinator to OpenRouter and to models Jev picks, avoiding the
  gateway defect above. To credit routing specifically, also benchmark a fixed
  frontier model through OpenRouter (for example `PROOF_ROUTER=off` with
  `PROOF_COORDINATOR_MODEL` set to it) as a control arm.
- **Sub-agents stay on the same gateway** in the Jev run, so their empty-
  argument failures will still affect both setups.
- **Sample size:** 3 reps per case shows large differences, not small ones.

To produce the side-by-side table once the Jev run exists:

```bash
uv run python scripts/bench_router.py --arms jev-router --reps 3 \
  --out data/bench/coordinator_jev_router.json
uv run python scripts/bench_router.py --summarize \
  data/bench/baseline_coordinator_off.json data/bench/coordinator_jev_router.json
```
