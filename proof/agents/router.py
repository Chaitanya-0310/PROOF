"""Per-decision model routing: which model should serve THIS turn?

The coordinator still plans, delegates and composes. What changes is that the
model behind each of those jobs is no longer one fixed constant in config.py:
before the coordinator starts, and before each sub-agent starts, a router
picks a tier for that specific piece of work.

Three modes, selected by PROOF_ROUTER and read at call time (so a benchmark
can switch arms in-process):

  off    -- today's behaviour. The configured COORDINATOR/SUBAGENT model,
            always. The baseline every other mode is measured against.
  light  -- ALWAYS the light tier. No intelligence at all. This is the
            control arm: without it, "the router made it faster" cannot be
            told apart from "the cheaper model is faster", and only the
            first is a claim about routing.
  jev    -- TypeSafe's Jev decision model (via OpenRouter's Decisions API)
            chooses light or strong per decision, with a confidence.

Two safety rules, because in this project a wrong number is worse than a slow
one:

  * Low confidence escalates. If Jev is below PROOF_ROUTER_MIN_CONFIDENCE the
    turn goes to the STRONG tier, not to Jev's pick.
  * Failure is fail-safe, not fail-cheap. Any router error or timeout routes
    to the strong tier -- i.e. to exactly what the system did before routing
    existed -- and the reason is recorded.

Data minimisation: Jev is sent the question text, the role, and the domain's
charter. It is NOT sent tool results, plant rows or SOP text -- a routing
decision does not need them, and every byte sent to a third party is a byte a
food manufacturer's security review has to approve.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .timing import current_timeline

JEV_URL = "https://openrouter.ai/api/alpha/decisions"

_TIER_CRITERIA = {
    "light": (
        "A direct lookup: one well-specified fact or list from one system "
        "(e.g. 'which lines are down at TOR1', 'current stock of flour'). "
        "Little or no arithmetic, no cross-checking, no recommendation."
    ),
    "strong": (
        "Needs multi-step reasoning: combining several results; arithmetic "
        "across a deadline, rate and schedule (e.g. whether staged dough "
        "expires before a line restarts); deciding which systems to consult; "
        "following safety, allergen or authorization rules; or composing a "
        "recommendation with tradeoffs for a plant manager."
    ),
}

_ROLE_NOTE = {
    "coordinator": (
        "Role: COORDINATOR. This model plans which domain agents to ask, "
        "then composes their answers into one decision for a plant manager."
    ),
    "subagent": (
        "Role: DOMAIN SUB-AGENT. This model answers one sub-question from its "
        "own domain's tools and reports figures verbatim; it does not compose "
        "the final answer."
    ),
}


@dataclass
class RouteDecision:
    model: str
    tier: str                      # light | strong | fixed
    mode: str                      # off | light | jev
    reason: str
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_s: float = 0.0
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RouterSettings:
    mode: str
    scope: str                     # all | subagents
    light_model: str
    strong_model: str | None       # None -> the caller's configured default
    min_confidence: float
    jev_model: str
    timeout_s: float
    api_key: str | None


def settings() -> RouterSettings:
    """Read router config from the environment at call time."""
    return RouterSettings(
        mode=(os.getenv("PROOF_ROUTER") or "off").strip().lower(),
        scope=(os.getenv("PROOF_ROUTER_SCOPE") or "all").strip().lower(),
        light_model=os.getenv("PROOF_ROUTER_LIGHT_MODEL") or "deepseek-v4-flash-0731",
        strong_model=os.getenv("PROOF_ROUTER_STRONG_MODEL") or None,
        min_confidence=float(os.getenv("PROOF_ROUTER_MIN_CONFIDENCE") or 0.6),
        jev_model=os.getenv("PROOF_JEV_MODEL") or "typesafe/jev-1.13",
        timeout_s=float(os.getenv("PROOF_ROUTER_TIMEOUT_S") or 5.0),
        api_key=os.getenv("OPENROUTER_API_KEY") or None,
    )


async def route(role: str, default_model: str, question: str, *,
                domain: str | None = None, charter: str | None = None
                ) -> RouteDecision:
    """Choose the model for one coordinator session or one delegation."""
    s = settings()
    strong = s.strong_model or default_model

    if s.mode == "off":
        return RouteDecision(default_model, "fixed", "off", "router off")
    if role == "coordinator" and s.scope == "subagents":
        return RouteDecision(default_model, "fixed", s.mode,
                             "coordinator not in router scope")
    if s.mode == "light":
        return RouteDecision(s.light_model, "light", "light",
                             "control arm: always the light tier")
    if s.mode != "jev":
        return RouteDecision(strong, "strong", s.mode,
                             f"unknown PROOF_ROUTER={s.mode!r}; fail-safe to strong")
    if not s.api_key:
        return RouteDecision(strong, "strong", "jev",
                             "OPENROUTER_API_KEY not set; fail-safe to strong")

    tl = current_timeline()
    start = time.perf_counter()
    try:
        answer, cost = await _ask_jev(s, role, question, domain, charter)
    except Exception as exc:  # noqa: BLE001 -- routing must never break the answer
        latency = time.perf_counter() - start
        if tl is not None:
            tl.add("router", f"{role}" + (f":{domain}" if domain else ""),
                   "jev-error", start, start + latency)
        return RouteDecision(strong, "strong", "jev",
                             f"router error ({type(exc).__name__}: {exc}); "
                             f"fail-safe to strong", latency_s=latency)
    latency = time.perf_counter() - start
    if tl is not None:
        tl.add("router", f"{role}" + (f":{domain}" if domain else ""),
               s.jev_model, start, start + latency)

    tier = answer.get("choice")
    conf = answer.get("confidence")
    probs = answer.get("probabilities") or {}
    if tier not in ("light", "strong"):
        return RouteDecision(strong, "strong", "jev",
                             f"unexpected choice {tier!r}; fail-safe to strong",
                             conf, probs, latency, cost)
    if conf is not None and conf < s.min_confidence:
        return RouteDecision(strong, "strong", "jev",
                             f"Jev chose {tier} at confidence {conf:.2f} "
                             f"< {s.min_confidence:.2f}; escalated to strong",
                             conf, probs, latency, cost)
    model = s.light_model if tier == "light" else strong
    return RouteDecision(model, tier, "jev", f"Jev chose {tier}",
                         conf, probs, latency, cost)


async def _ask_jev(s: RouterSettings, role: str, question: str,
                   domain: str | None, charter: str | None
                   ) -> tuple[dict[str, Any], float]:
    """One Decisions API call. Returns (choice answer, usd cost)."""
    import httpx

    state = [_ROLE_NOTE.get(role, f"Role: {role}.")]
    if domain:
        state.append(f"Domain: {domain}. Charter: {charter or ''}")
    state.append(f"Request: {question}")

    body = {
        "model": s.jev_model,
        "state": "\n".join(state),
        "questions": {
            "tier": {
                "type": "choice",
                "instructions": (
                    "Which model tier should handle this request for a bakery "
                    "manufacturing operations assistant? A wrong figure is "
                    "worse than a slow one, so pick 'strong' whenever the "
                    "work involves more than a direct lookup."),
                "criteria": _TIER_CRITERIA,
            }
        },
    }
    async with httpx.AsyncClient(timeout=s.timeout_s) as client:
        resp = await client.post(
            JEV_URL, json=body,
            headers={"Authorization": f"Bearer {s.api_key}"})
        resp.raise_for_status()
        data = resp.json()
    answer = (data.get("answers") or {}).get("tier") or {}
    cost = float((data.get("usage") or {}).get("cost") or 0.0)
    return answer, cost
