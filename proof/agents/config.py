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

_CLAUDE_ONLY = ("thinking", "betas", "fallbacks")

if BASE_URL:
    # Adaptive thinking, the fallback beta and `fallbacks` are Claude API
    # features. A compatible gateway may reject them outright, so they are
    # dropped rather than risk every request failing on an unknown field.
    for _key in _CLAUDE_ONLY:
        REQUEST_KWARGS.pop(_key)

# The coordinator under PROOF_ROUTER=jev-router talks to OpenRouter whatever
# BASE_URL says, and Jev chooses the reasoning effort itself -- so the same
# Claude-only fields are withheld there too.
OPENROUTER_REQUEST_KWARGS: dict = {k: v for k, v in REQUEST_KWARGS.items()
                                   if k not in _CLAUDE_ONLY}

# Rough per-million-token rates for Claude Opus 5, used only for the local
# cost line the CLI prints. Phase 5 replaces this with real per-span
# accounting; it is here now because "what did that question cost?" is a
# question an operations director asks on day one.
# Override both when running a different model, or every cost line is wrong.
# `or`, not a getenv default: a blank line copied from .env.example means
# "unset", and float("") would crash at import.
USD_PER_MTOK_IN = float(os.getenv("PROOF_USD_PER_MTOK_IN") or 5.00)
USD_PER_MTOK_OUT = float(os.getenv("PROOF_USD_PER_MTOK_OUT") or 25.00)


# Per-model rates, for when a router mixes models in one question. One global
# rate is fine while every call uses the same model; the moment a router sends
# some turns elsewhere it makes every cost line wrong in a way that looks
# plausible. JSON: {"model-id": [usd_per_mtok_in, usd_per_mtok_out], ...}.
# A model missing from the table falls back to the global rate above, and
# `price_known()` lets a report say so rather than print a confident guess.
def _parse_model_prices() -> dict[str, tuple[float, float]]:
    import json

    raw = os.getenv("PROOF_MODEL_PRICES") or ""
    if not raw.strip():
        return {}
    try:
        return {k: (float(v[0]), float(v[1])) for k, v in json.loads(raw).items()}
    except (ValueError, TypeError, IndexError, AttributeError):
        return {}


MODEL_PRICES: dict[str, tuple[float, float]] = _parse_model_prices()


def price_known(model: str | None) -> bool:
    return model is None or model in MODEL_PRICES or model in (
        COORDINATOR_MODEL, SUBAGENT_MODEL)


def estimate_cost(input_tokens: int, output_tokens: int,
                  model: str | None = None) -> float:
    rate_in, rate_out = MODEL_PRICES.get(model or "", (USD_PER_MTOK_IN, USD_PER_MTOK_OUT))
    return (input_tokens / 1_000_000 * rate_in
            + output_tokens / 1_000_000 * rate_out)
