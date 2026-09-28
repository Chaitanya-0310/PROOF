"""Where the time goes in one question.

The CLI has always printed one number -- total seconds -- and one number
cannot tell you what to fix. A slow answer might be the model thinking, the
MCP servers starting, SQL, or Python orchestration between them, and each of
those has a different remedy. A model router can only ever shorten the FIRST
one, so before swapping in a router it is worth knowing how big that slice is.

Every interval is captured from OUTSIDE the code it measures, so the numbers
cannot drift from what actually ran:

  * model calls   -- httpx request/response hooks on the SDK's own client.
                     Every HTTP round trip is timed, retries included, and
                     attributed to whichever agent was running when it left.
  * tool calls    -- a thin proxy over the MCP ClientSession that times
                     `call_tool`, which is the only door the SDK's MCP bridge
                     uses.
  * startup       -- MCP server spawn + initialise, before any planning.
  * router        -- the model-selection decision, when a router is on.

Agents run concurrently (the coordinator may ask several at once), so summed
seconds can exceed wall-clock seconds. The report gives both: the SUM says how
much work was done, the WALL (interval union) says how much of the user's wait
it accounted for. "orchestration" is whatever wall time no interval covers --
Python, stdio framing, JSON -- and is the honest residual, not a guess.
"""
from __future__ import annotations

import contextvars
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# Which agent and model the code on this task is currently acting for. Set by
# the coordinator loop and by each sub-agent; read by the HTTP hook to
# attribute a model call. contextvars, not globals, so concurrent questions in
# the API server (one asyncio task each) never see each other's timelines.
_current_agent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "proof_current_agent", default="unattributed")
_current_model: contextvars.ContextVar[str] = contextvars.ContextVar(
    "proof_current_model", default="")
_current_timeline: contextvars.ContextVar["Timeline | None"] = contextvars.ContextVar(
    "proof_current_timeline", default=None)


@dataclass
class Interval:
    kind: str          # startup | model | tool | router
    agent: str         # coordinator | subagent:<domain> | session
    label: str         # model id, tool name, or a short description
    start: float
    end: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        return self.end - self.start


class Timeline:
    """Collects intervals for one question and summarises them."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.t_end: float | None = None
        self.intervals: list[Interval] = []

    def add(self, kind: str, agent: str, label: str,
            start: float, end: float, **meta: Any) -> None:
        self.intervals.append(Interval(kind, agent, label, start, end, meta))

    @contextmanager
    def measure(self, kind: str, label: str, agent: str | None = None, **meta: Any):
        start = time.perf_counter()
        try:
            yield meta
        finally:
            self.add(kind, agent or _current_agent.get(), label,
                     start, time.perf_counter(), **meta)

    def finish(self) -> None:
        self.t_end = time.perf_counter()

    # -- summary ----------------------------------------------------------
    def report(self) -> dict[str, Any]:
        end = self.t_end or time.perf_counter()
        total = end - self.t0

        def of(kind: str) -> list[Interval]:
            return [i for i in self.intervals if i.kind == kind]

        model, tools, router = of("model"), of("tool"), of("router")
        coord = [i for i in model if i.agent == "coordinator"]
        sub = [i for i in model if i.agent.startswith("subagent:")]
        per_call = [round(i.seconds, 3) for i in model]

        by_agent: dict[str, dict[str, Any]] = {}
        for i in model + tools:
            a = by_agent.setdefault(i.agent, {"model_s": 0.0, "model_calls": 0,
                                              "tool_s": 0.0, "tool_calls": 0})
            if i.kind == "model":
                a["model_s"] += i.seconds
                a["model_calls"] += 1
            else:
                a["tool_s"] += i.seconds
                a["tool_calls"] += 1
        for a in by_agent.values():
            a["model_s"] = round(a["model_s"], 3)
            a["tool_s"] = round(a["tool_s"], 3)

        covered = _union(self.intervals)
        return {
            "total_s": round(total, 3),
            "startup_s": round(sum(i.seconds for i in of("startup")), 3),
            "teardown_s": round(sum(i.seconds for i in of("teardown")), 3),
            "model": {
                "calls": len(model),
                "sum_s": round(sum(i.seconds for i in model), 3),
                "wall_s": round(_union(model), 3),
                "coordinator_s": round(sum(i.seconds for i in coord), 3),
                "subagent_s": round(sum(i.seconds for i in sub), 3),
                "median_call_s": round(statistics.median(per_call), 3) if per_call else 0.0,
                "max_call_s": max(per_call) if per_call else 0.0,
                "per_call": [{"agent": i.agent, "model": i.label,
                              "s": round(i.seconds, 3),
                              "status": i.meta.get("status")} for i in model],
            },
            "tools": {
                "calls": len(tools),
                "sum_s": round(sum(i.seconds for i in tools), 3),
                "wall_s": round(_union(tools), 3),
            },
            "router": {
                "decisions": len(router),
                "sum_s": round(sum(i.seconds for i in router), 3),
                "wall_s": round(_union(router), 3),
            },
            # Wall time no measured interval accounts for.
            "orchestration_s": round(max(0.0, total - covered), 3),
            "by_agent": by_agent,
        }


def _union(intervals: list[Interval]) -> float:
    """Length of the union of intervals -- wall time, overlaps counted once."""
    spans = sorted((i.start, i.end) for i in intervals)
    total, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


# --- agent attribution -----------------------------------------------------
@contextmanager
def acting_as(agent: str, model: str):
    """Attribute model calls made inside this block to `agent` / `model`."""
    t_agent = _current_agent.set(agent)
    t_model = _current_model.set(model)
    try:
        yield
    finally:
        _current_agent.reset(t_agent)
        _current_model.reset(t_model)


@contextmanager
def timeline_scope(timeline: Timeline):
    token = _current_timeline.set(timeline)
    try:
        yield timeline
    finally:
        _current_timeline.reset(token)


def current_timeline() -> Timeline | None:
    return _current_timeline.get()


# --- model-call timing: httpx hooks ---------------------------------------
async def _on_request(request) -> None:
    request.extensions["proof_t0"] = time.perf_counter()
    request.extensions["proof_agent"] = _current_agent.get()
    request.extensions["proof_model"] = _current_model.get()


async def _on_response(response) -> None:
    tl = _current_timeline.get()
    ext = response.request.extensions
    t0 = ext.get("proof_t0")
    # Only the Messages endpoint is a model call; anything else the client
    # might send (model listings, count_tokens) is not model time.
    if tl is None or t0 is None or "/messages" not in response.request.url.path:
        return
    # Non-streaming: the server finishes generating before it sends headers,
    # so request -> response-headers is the model's latency, and the body
    # read that follows is negligible by comparison.
    tl.add("model", ext.get("proof_agent", "unattributed"),
           ext.get("proof_model") or "?", t0, time.perf_counter(),
           status=response.status_code)


def timed_http_client():
    """An SDK-default async httpx client with timing hooks attached.

    DefaultAsyncHttpxClient keeps the SDK's own timeouts and connection
    limits; only the event hooks are added.
    """
    from anthropic import DefaultAsyncHttpxClient

    return DefaultAsyncHttpxClient(
        event_hooks={"request": [_on_request], "response": [_on_response]})


# --- tool-call timing: MCP session proxy ----------------------------------
class TimedSession:
    """Wraps an MCP ClientSession so every `call_tool` is timed.

    The SDK's MCP bridge (`async_mcp_tool`) only ever calls
    `call_tool(name=..., arguments=...)`; everything else is forwarded
    untouched.
    """

    def __init__(self, session: Any, agent: str) -> None:
        self._session = session
        self._agent = agent

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def call_tool(self, name: str, arguments: Any = None, *args: Any, **kwargs: Any):
        start = time.perf_counter()
        try:
            return await self._session.call_tool(name=name, arguments=arguments,
                                                 *args, **kwargs)
        finally:
            tl = _current_timeline.get()
            if tl is not None:
                tl.add("tool", self._agent, name, start, time.perf_counter())


def format_report(r: dict[str, Any]) -> str:
    """The breakdown the CLI prints under the answer."""
    m, t, ro = r["model"], r["tools"], r["router"]
    total = r["total_s"] or 1e-9

    def pct(x: float) -> str:
        return f"{100 * x / total:4.0f}%"

    head = f"time breakdown  (total {r['total_s']:.1f}s wall"
    if "time_to_answer_s" in r:
        head += f", answer ready at {r['time_to_answer_s']:.1f}s"
    lines = [
        head + ")",
        f"  startup (MCP spawn)   {r['startup_s']:6.1f}s  {pct(r['startup_s'])}",
        f"  model calls           {m['wall_s']:6.1f}s  {pct(m['wall_s'])}   "
        f"{m['calls']} calls, median {m['median_call_s']:.1f}s, max {m['max_call_s']:.1f}s",
        f"    coordinator         {m['coordinator_s']:6.1f}s  (sum)",
        f"    sub-agents          {m['subagent_s']:6.1f}s  (sum)",
        f"  tool calls (MCP+SQL)  {t['wall_s']:6.1f}s  {pct(t['wall_s'])}   {t['calls']} calls",
    ]
    if ro["decisions"]:
        lines.append(f"  router decisions      {ro['wall_s']:6.1f}s  {pct(ro['wall_s'])}   "
                     f"{ro['decisions']} decisions")
    lines.append(f"  MCP shutdown          {r['teardown_s']:6.1f}s  {pct(r['teardown_s'])}")
    lines.append(f"  orchestration/other   {r['orchestration_s']:6.1f}s  {pct(r['orchestration_s'])}")
    if m["sum_s"] > m["wall_s"] + 0.05:
        lines.append(f"  (model sum {m['sum_s']:.1f}s > wall {m['wall_s']:.1f}s: "
                     f"agents ran in parallel)")
    return "\n".join(lines)
