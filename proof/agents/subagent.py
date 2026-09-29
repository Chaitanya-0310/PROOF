"""A domain sub-agent: one agent, one MCP server, one narrow job.

Each sub-agent connects to exactly ONE MCP server and therefore sees only
that domain's tools -- 3 to 6 of them. That narrowness is the entire reason
the multi-agent split exists. A single agent holding all 18 tools has to
discriminate between `estimate_output_loss` and `project_wip_expiry` on every
turn; the production agent never sees the second one and cannot get it wrong.

The sub-agent answers a natural-language sub-question and returns prose plus
the tool calls it made. It does NOT compose the final answer -- that is the
coordinator's job. Keeping sub-agents from editorialising is what stops four
partial answers turning into four competing conclusions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession

from proof.rag.citations import check, citations_from_tool_results, enforce
from proof.trace import record_usage, span

from .budget import SessionBudget, billed_cost
from .loopguard import LoopGuard, required_args
from .config import REQUEST_KWARGS, SUBAGENT_MODEL
from .servers import ServerSpec
from .timing import TimedSession, acting_as

SUBAGENT_SYSTEM = """\
You are the {domain} agent for a bakery manufacturing network.

Your charter:
{charter}

Rules:
- Answer ONLY from your tools. Never estimate, recall or infer a plant number.
  If your tools cannot answer, say exactly what is missing and stop.
- Stay inside your domain. If the question needs another domain's data, say
  so plainly rather than guessing -- the coordinator will route it.
- Report figures exactly as the tools return them. Do not round, and do not
  convert units unless a tool did.
- Be terse. You are reporting to a coordinator that will compose the final
  answer, not to a human. No preamble, no restating the question.
- Each tool's `summary` and first rows are forwarded to the coordinator
  automatically, so you need not restate them row by row. Do state totals
  from `summary` when there is one; never present a row count as a total.
- Name the identifiers a follow-up would need (line_id, run_id, sku_id,
  sku_code, run_ids).
- When a tool result includes a `note`, treat it as a binding caveat and
  reflect it in your answer.
- You MUST briefly explain your reasoning in text before making any tool call.
"""


@dataclass
class SubAgentResult:
    """What a sub-agent hands back, including its working."""
    domain: str
    answer: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    # Citation enforcement outcome: ok | fabricated | uncited | n/a.
    # 'n/a' means nothing citable was retrieved, which is the normal case for
    # the structured tools that answer from SQL.
    citation_status: str = "n/a"
    citations_used: list[str] = field(default_factory=list)
    citations_fabricated: list[str] = field(default_factory=list)
    # 'denied' if any tool this agent called was refused by the authz layer.
    # Surfaced up so the eval can grade "was the unauthorised action refused"
    # without re-querying the denial log.
    authorization: str = "allowed"
    # Which model actually served this delegation, and why (the router's
    # decision). Recorded so a benchmark -- or a reviewer -- can see which
    # tier produced each answer rather than assume.
    model: str = ""
    route: dict[str, Any] = field(default_factory=dict)

    def as_tool_result(self) -> str:
        """Rendered for the coordinator.

        The SQL is carried up deliberately. The coordinator is instructed to
        cite it, and a plant manager who cannot see the query behind a number
        has no way to challenge it -- which is how these systems lose trust
        the first time they are wrong.
        """
        header = f"[{self.domain} agent]"
        if self.citation_status == "ok":
            header += f"  (citations verified: {', '.join(self.citations_used)})"
        elif self.citation_status == "fabricated":
            header += ("  (CITATION CHECK FAILED -- answer withheld; "
                       f"fabricated: {', '.join(self.citations_fabricated)})")
        elif self.citation_status == "uncited":
            header += "  (UNVERIFIED -- procedural claims with no citation)"

        lines = [header, self.answer]
        if self.tool_calls:
            lines.append("\nEvidence:")
            for c in self.tool_calls:
                rows = c["row_count"]
                shown = f"{rows} row(s)" if rows is not None else "rows unknown"
                lines.append(f"- {c['tool']}({json.dumps(c['args'])}) -> {shown}")
                if c.get("sql"):
                    lines.append(f"  SQL: {c['sql']}")
                elif c.get("evidence_error"):
                    # Say so rather than printing a bare "None". A missing
                    # audit trail is itself worth reporting upward.
                    lines.append(f"  (evidence unavailable: {c['evidence_error']})")
                lines.extend(_data_lines(c))
        return "\n".join(lines)


# How much of each tool result travels up verbatim. The sub-agent is told to
# be terse, and it was: a 40-row runout came back as prose naming one run, so
# the coordinator rendered an impact table of dashes and reported that the
# data "was not returned". The rows were there; the summary dropped them.
DATA_ROWS_UP = 20
DATA_CELL_CHARS = 160


def _data_lines(call: dict[str, Any]) -> list[str]:
    """A tool's `summary` and its first rows, compact, for the coordinator."""
    out: list[str] = []
    if call.get("summary"):
        out.append(f"  summary: {json.dumps(call['summary'], default=str)}")
    rows = call.get("rows") or []
    if rows:
        shown = rows[:DATA_ROWS_UP]
        out.append(f"  rows ({len(shown)} of {len(rows)}):")
        for r in shown:
            out.append("    " + json.dumps(
                {k: (v[:DATA_CELL_CHARS] + "…"
                     if isinstance(v, str) and len(v) > DATA_CELL_CHARS else v)
                 for k, v in r.items()}, default=str))
    return out


async def run_subagent(spec: ServerSpec, session: ClientSession,
                       question: str, client: AsyncAnthropic,
                       budget: "SessionBudget | None" = None,
                       streamer: Any = None,
                       model: str | None = None,
                       request_kwargs: dict | None = None) -> SubAgentResult:
    """Run one domain sub-agent against its MCP server.

    A `delegate` span wraps the whole call and every turn's tokens roll up
    into it, so the trace answers "what did asking the inventory agent cost?"
    The shared budget is checked after each turn, so a sub-agent stuck in a
    loop trips the ceiling here rather than spinning to its max_iterations.

    `model` is the router's pick for this delegation; None means the
    configured SUBAGENT_MODEL, i.e. behaviour without a router.
    `request_kwargs` overrides REQUEST_KWARGS when `client` is a different
    provider (OpenRouter) that must not receive Claude-only fields.
    """
    model = model or SUBAGENT_MODEL
    request_kwargs = REQUEST_KWARGS if request_kwargs is None else request_kwargs
    agent = f"subagent:{spec.domain}"
    mcp_tools = (await session.list_tools()).tools
    # TimedSession times each call_tool for the per-question breakdown;
    # everything else passes straight through to the real session.
    timed = TimedSession(session, agent)
    tools = [async_mcp_tool(t, timed) for t in mcp_tools]
    guard = LoopGuard(required_args({t.name: t.input_schema for t in mcp_tools}))

    calls: list[dict[str, Any]] = []
    final_text: list[str] = []
    raw_results: list[str] = []
    in_tok = out_tok = 0

    with span(f"delegate:{spec.domain}", "delegate",
              domain=spec.domain, model=model) as sp, acting_as(agent, model):
        runner = client.beta.messages.tool_runner(
            system=SUBAGENT_SYSTEM.format(domain=spec.domain, charter=spec.charter),
            messages=[{"role": "user", "content": question}],
            tools=tools,
            model=model,
            # A sub-agent that needs more than this many round trips has
            # misunderstood its job; better to stop than to spin on the bill.
            max_iterations=8,
            **request_kwargs,
        )

        async for message in runner:
            in_tok += message.usage.input_tokens
            out_tok += message.usage.output_tokens
            if budget is not None:
                budget.record(message.usage.input_tokens,
                              message.usage.output_tokens,
                              getattr(message, "model", None) or model,
                              billed_usd=billed_cost(message))
                budget.check()  # raises BudgetExceeded; caught by coordinator

            # Break out before the runner executes this turn's calls: they
            # would fail (or return) exactly as they did last turn.
            stuck = guard.check(message)
            if stuck:
                sp.set_attribute("proof.stuck", stuck)
                final_text = [f"[stopped: {stuck}. The {spec.domain} agent "
                              f"has no reliable result for this question.]"]
                break

            text_blocks = [b.text.strip() for b in message.content
                           if b.type == "text" and b.text.strip()]
            thinking_blocks = [getattr(b, "thinking", "").strip() for b in message.content
                               if b.type == "thinking" and getattr(b, "thinking", "").strip()]
            
            explanations = thinking_blocks or text_blocks
            if text_blocks:
                final_text = text_blocks

            turn_calls = [b for b in message.content if b.type == "tool_use"]
            for block in turn_calls:
                explanation = "\n".join(explanations) if explanations else None
                calls.append({"tool": block.name, "args": block.input,
                              "explanation": explanation,
                              "row_count": None, "sql": None})
                if streamer:
                    await streamer.emit("tool_call", {
                        "domain": spec.domain,
                        "tool": block.name,
                        "args": block.input,
                        "explanation": explanation
                    })

            # Pull the tool results for THIS turn. generate_tool_call_response
            # is the runner's only public window onto what the tools returned;
            # an earlier version read a non-existent `.messages` attribute
            # inside a bare `except: pass`, so every result came back sql=None
            # and nothing complained. The call is cached, so this does not
            # re-run the tools.
            if turn_calls:
                raw_results.extend(
                    await _attach_turn_results(runner, calls[-len(turn_calls):]))

        answer = "\n".join(final_text) or "(no answer produced)"

        # Citation enforcement. The system prompt ASKS for citations; this
        # CHECKS them. An answer citing a section retrieval never returned is
        # replaced outright -- a fabricated reference is worse than no answer,
        # because the reference is what makes a wrong procedure credible.
        available = citations_from_tool_results(raw_results)
        citation_check = check(answer, available)
        answer = enforce(answer, citation_check)

        # Did the authz layer refuse any tool this agent called? The action
        # tools return {"authorization": "denied"} in their result payload.
        authorization = "allowed"
        for raw in raw_results:
            try:
                if json.loads(raw).get("authorization") == "denied":
                    authorization = "denied"
                    break
            except (ValueError, TypeError):
                continue

        record_usage(sp, in_tok, out_tok, model)
        sp.set_attribute("proof.tool_calls", len(calls))
        sp.set_attribute("proof.citation_status", citation_check.status)

    return SubAgentResult(
        domain=spec.domain,
        answer=answer,
        tool_calls=calls,
        input_tokens=in_tok,
        output_tokens=out_tok,
        citation_status=citation_check.status,
        citations_used=citation_check.cited,
        citations_fabricated=citation_check.fabricated,
        authorization=authorization,
        model=model,
    )


async def _attach_turn_results(runner: Any,
                         turn_calls: list[dict[str, Any]]) -> list[str]:
    """Attach row_count and SQL from this turn's tool results.

    Tool results are JSON text produced by proof.tools._base.query, so each
    one carries its own SQL. Parsing is defensive -- a malformed result should
    cost us the evidence line, not the answer -- but it records WHY it failed
    rather than swallowing it, because that is how the previous version of
    this stayed broken.
    """
    try:
        response = await runner.generate_tool_call_response()
    except Exception as exc:  # noqa: BLE001
        for call in turn_calls:
            call["evidence_error"] = f"{type(exc).__name__}: {exc}"
        return []

    if response is None:
        for call in turn_calls:
            call["evidence_error"] = "runner returned no tool response"
        return []

    content = response.get("content") if isinstance(response, dict) else None
    if not isinstance(content, list):
        for call in turn_calls:
            call["evidence_error"] = f"unexpected response shape {type(content).__name__}"
        return []

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            texts.append(inner)
        elif isinstance(inner, list):
            for ib in inner:
                if isinstance(ib, dict) and ib.get("type") == "text":
                    texts.append(ib["text"])

    for call, raw in zip(turn_calls, texts):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            call["evidence_error"] = "tool result was not JSON"
            continue
        call["row_count"] = payload.get("row_count")
        call["sql"] = payload.get("sql")
        call["rows"] = payload.get("rows")
        call["summary"] = payload.get("summary")

    # Returned so the caller can run citation validation against the exact
    # rows retrieval produced, rather than against a re-query.
    return texts
