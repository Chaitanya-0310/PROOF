# Coordinator routing: baseline vs Jev (2026-09-28)

Three ways of choosing the coordinator's model, on the same 4 golden tasks × 3
reps. Sub-agents stayed on `gpt-5.6-luna` via the cheaperinference gateway in
every arm. Baseline details: [coordinator_baseline.md](coordinator_baseline.md).

| Arm | What decides the coordinator model | Where it runs | Raw data |
|---|---|---|---|
| `off` | fixed: `gpt-5.6-luna` | gateway | `data/bench/baseline_coordinator_off.json` (18:18–18:34) |
| `jev-router` | OpenRouter's `typesafe/jev-router` picks any model | OpenRouter | `data/bench/coordinator_jev_router.json` |
| `jev` (scope `coordinator`) | Jev decision API picks light (`deepseek-v4-flash-0731`) or strong (`gpt-5.6-luna`) | gateway | `data/bench/coordinator_jev_gateway.json` (19:30–19:52) |

## Results

| | `off` | `jev-router` | `jev` on gateway |
|---|---|---|---|
| Runs finished | 12/12 | 9/12 (3 × OpenRouter credit limit, HTTP 402) | 12/12 |
| Produced an answer | 6/12 | 9/9 | 12/12 |
| **Answered and passed, out of 12** | **6** | **8** | **11** |
| Answer time, median | 67 s | 192 s | 127 s |
| Coordinator model time, median | 39 s | 6 s | 28 s |
| Model the coordinator actually ran | luna | DeepSeek v4.1 Flash (29 of 39 calls; 10 unreported) | **luna, 12 of 12** |
| Cost per question, mean | $0.0013 | not reliable (priced before billed-cost tracking) | $0.0022 |

Per task, passes out of 3:

| Task | `off` | `jev-router` | `jev` |
|---|---|---|---|
| line_down_hero | 1 | 3 | 2 |
| supplier_slip | 2 | 2 of 2 | 3 |
| restart_procedure | 1 | 1 of 2 | 3 |
| single_domain_balance | 3 | 2 of 2 | 3 |

## What this actually shows

**The `jev` arm never changed the model.** Jev chose "strong" for every
multi-agent question and was unsure on the simple lookup (confidence 0.00–0.09),
which the safety rule escalates to strong. So all 12 `jev` runs used
`gpt-5.6-luna`, exactly like the baseline.

**Yet it scored 11/12 against the baseline's 6/12.** Same model, same gateway,
same tasks. The only difference is *when* the runs happened. The baseline's
failures were the gateway returning tool calls with empty arguments
(documented in the baseline doc); in the 19:30 window that barely happened.

**Conclusion: the gateway's reliability changes over time, and that variation
is larger than any effect of routing.** None of these arms can credit Jev with
the improvement:

- `jev` ran the same model as `off`, so its gain is gateway variation.
- `jev-router` also avoided the gateway for the coordinator, so its gain may be
  the provider, not routing (the `openrouter-fixed` control is still to run).

## Other findings

- **Jev does not see the coordinator's job as light work** on this gateway's
  pair of models. With `light` = `deepseek-v4-flash-0731`, routing the
  coordinator saves nothing, because it always escalates. The earlier
  "always light" arm (`router_off_vs_light.json`) scored 1/5 on this model, so
  that caution looks justified.
- **Jev's decisions are cheap and fast:** about $0.00002 and 0.36 s each.
- **The one `jev` failure** (line_down_hero rep 3) skipped `project_wip_expiry`,
  so it missed the dough-scrap loss. This is the failure the coordinator
  prompt's "two losses" rule exists to prevent, and it still happens with luna.
- **Latency is dominated by sub-agents** in every arm that does the full job
  (`jev`: 74 s sub-agent vs 28 s coordinator, median).

## How to get a result you can trust

`scripts/bench_router.py` is designed to run arms **interleaved in one
session**, rotating order per task, precisely so time-varying provider
behaviour affects every arm equally. The three arms above were run in separate
sessions, so they don't have that protection. Re-run them together:

```bash
PROOF_ROUTER_SCOPE=coordinator uv run python scripts/bench_router.py \
  --arms off,jev --reps 3 --out data/bench/coordinator_interleaved.json
```

Add `jev-router,openrouter-fixed` to `--arms` once the OpenRouter key has
credit; that pair isolates routing from the provider change.
