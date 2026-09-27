"""Model and request configuration, in one place.

Phase 2 runs everything on one frontier model. Phase 4 introduces the hybrid
split -- a self-hosted open-weight model for routine routing, the frontier
model only for planning and synthesis -- and when it does, this is the file
that changes.
"""
from __future__ import annotations

import os

from proof.db import load_env

# Read .env before any setting below. Callers such as cli.py import this
# module before loading .env themselves, and a model name sitting in .env
# would otherwise be silently ignored in favour of the default.
load_env()

# Claude Opus 5 for both tiers in Phase 2. Resisting a cheap-worker split
# until there is an eval (Phase 5) is deliberate: without measurement, a
# model downgrade is a guess about quality, and the failure mode -- a
# sub-agent that quietly picks the wrong tool -- is invisible in a demo.
COORDINATOR_MODEL = os.getenv("PROOF_COORDINATOR_MODEL") or "claude-opus-5"
SUBAGENT_MODEL = os.getenv("PROOF_SUBAGENT_MODEL") or "claude-opus-5"

# Set when the Anthropic SDK is pointed at a third-party gateway that speaks
# the Messages API (e.g. a DeepSeek reseller). The agent loop is unchanged;
# only the Claude-specific request features below are withheld.
BASE_URL = os.getenv("ANTHROPIC_BASE_URL")

# Shared across coordinator and sub-agents.
REQUEST_KWARGS: dict = {
    "max_tokens": int(os.getenv("PROOF_MAX_TOKENS") or 16000),
    # Adaptive thinking: the model decides when and how deeply to reason.
    # This work genuinely needs it -- the scrap-clock question requires
    # holding a rate, a deadline and a schedule together.
    "thinking": {"type": "adaptive"},
    # Server-side fallback. Claude Opus 5 may decline a request with
    # stop_reason "refusal"; "default" routes by refusal category so there is
    # no model list to maintain. Without this a refusal surfaces as an empty
    # answer with HTTP 200, which in an ops tool reads as "nothing is wrong".
    "betas": ["server-side-fallback-2026-07-01"],
    "fallbacks": "default",
}

if BASE_URL:
    # Adaptive thinking, the fallback beta and `fallbacks` are Claude API
    # features. A compatible gateway may reject them outright, so they are
    # dropped rather than risk every request failing on an unknown field.
    for _key in ("thinking", "betas", "fallbacks"):
        REQUEST_KWARGS.pop(_key)

# Rough per-million-token rates for Claude Opus 5, used only for the local
# cost line the CLI prints. Phase 5 replaces this with real per-span
# accounting; it is here now because "what did that question cost?" is a
# question an operations director asks on day one.
# Override both when running a different model, or every cost line is wrong.
# `or`, not a getenv default: a blank line copied from .env.example means
# "unset", and float("") would crash at import.
USD_PER_MTOK_IN = float(os.getenv("PROOF_USD_PER_MTOK_IN") or 5.00)
USD_PER_MTOK_OUT = float(os.getenv("PROOF_USD_PER_MTOK_OUT") or 25.00)


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000 * USD_PER_MTOK_IN
            + output_tokens / 1_000_000 * USD_PER_MTOK_OUT)
