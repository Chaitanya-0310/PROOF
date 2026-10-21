"""Retrieve-then-generate text-to-SQL.

    question -> route (top-k tables + join bridges + pinned clock)
             -> prompt with ONLY those tables' DDL
             -> generate one SELECT
             -> guard + execute (read-only)
             -> repair on error, at most MAX_REPAIRS times
             -> answer from the rows

The repair loop is what makes a 4-table prompt safe to rely on. When the
router misses a table, the model either says CANNOT_ANSWER or references a
table it was not shown; both are detectable, and the retry widens the schema
slice rather than letting the model guess. When the database rejects a
column, its own error message goes back verbatim -- Postgres error text
("column l.code does not exist, perhaps you meant l.line_code") is the best
repair hint available.

The numbers in the answer come from the rows, and the SQL travels with the
answer, the same rule as every hand-written tool: a figure a plant manager
cannot audit is a figure they will not act on.

Run:  make sql Q="which plant had the most downtime minutes last week?"
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

from proof.agents.config import REQUEST_KWARGS, SUBAGENT_MODEL, estimate_cost
from proof.trace import record_usage, set_io, span

from . import guard
from .prompts import SQL_RULES
from .router import DEFAULT_TOP_K, RoutedTable, Routing, all_tables, describe, route

MAX_REPAIRS = 2
ANSWER_ROWS = 100       # rows shown to the answer step; totals come from SQL

GENERATE_SYSTEM = f"""\
You translate questions about a multi-plant bakery manufacturing network into
a single PostgreSQL 16 query. You are given only the part of the schema that
retrieval judged relevant.

Rules:
{SQL_RULES}

If the schema you were given cannot answer the question, do not guess or
approximate with a different table. Reply with one line:
CANNOT_ANSWER: <the data you would need>

Otherwise reply with the query in a single ```sql fenced block, followed by
one line starting "Assumptions:" naming any interpretation you made (time
window, how a term was mapped to a column), or "Assumptions: none"."""

ANSWER_SYSTEM = """\
You answer an operations question from the result of a SQL query that was
run against the plant database. You are writing for a plant manager.

- Use ONLY the rows given. Never estimate or add a figure that is not in them.
- Lead with the answer in one sentence. Then, if the result is a list,
  a short markdown table of the most important rows.
- If the result was truncated, say so and do not present the row count as a
  total.
- If the result is empty, say what was searched for and that nothing matched;
  do not speculate about why.
- State the assumptions the query made, briefly, at the end."""


@dataclass
class Attempt:
    sql: str | None
    error: str | None = None
    error_kind: str | None = None
    tables: list[str] = field(default_factory=list)


@dataclass
class Text2SQLResult:
    question: str
    routing: Routing
    status: str                         # ok | cannot_answer | failed
    sql: str | None = None
    assumptions: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    model: str = SUBAGENT_MODEL
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return estimate_cost(self.input_tokens, self.output_tokens, self.model)

    def as_dict(self) -> dict[str, Any]:
        return {"question": self.question, "status": self.status,
                "routing": self.routing.as_dict(), "sql": self.sql,
                "assumptions": self.assumptions,
                "row_count": self.result.get("row_count"),
                "truncated": self.result.get("truncated"),
                "rows": self.result.get("rows", [])[:ANSWER_ROWS],
                "answer": self.answer,
                "attempts": [a.__dict__ for a in self.attempts],
                "model": self.model,
                "tokens": {"input": self.input_tokens, "output": self.output_tokens},
                "cost_usd": round(self.cost_usd, 5)}


class _Usage:
    def __init__(self) -> None:
        self.inp = self.out = 0

    def add(self, msg: Any) -> None:
        self.inp += msg.usage.input_tokens
        self.out += msg.usage.output_tokens


def ask(question: str, *, top_k: int = DEFAULT_TOP_K, answer: bool = True,
        client: Anthropic | None = None, model: str | None = None) -> Text2SQLResult:
    """Answer a question by retrieving schema, writing SQL and running it."""
    model = model or SUBAGENT_MODEL
    client = client or Anthropic()
    usage = _Usage()

    with span("text2sql", "session", model=model) as sp:
        set_io(sp, input={"question": question, "top_k": top_k})
        routing = route(question, top_k)
        res = Text2SQLResult(question, routing, status="failed", model=model)

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": _generation_prompt(routing, question)}]
        widened = False
        for _ in range(MAX_REPAIRS + 1):
            msg = _call(client, model, GENERATE_SYSTEM, messages)
            usage.add(msg)
            # The whole content goes back, thinking blocks included: dropping
            # them invalidates the model's reasoning on the repair turn.
            messages.append({"role": "assistant", "content": msg.content})
            text = _text(msg)
            sql, assumptions, cannot = _parse(text)

            if cannot is not None:
                res.attempts.append(Attempt(None, cannot, "cannot_answer"))
                if widened:
                    res.status, res.answer = "cannot_answer", cannot
                    break
                # A router miss looks exactly like this. Retry once with a
                # wider slice before believing it.
                widened = True
                routing = route(question, top_k * 2)
                res.routing = routing
                messages.append({"role": "user", "content":
                                 "More of the schema, in case it helps:\n\n"
                                 f"{routing.schema_text()}\n\n"
                                 "Try again with the same rules. If it still "
                                 "cannot be answered, reply CANNOT_ANSWER again."})
                continue
            if sql is None:
                res.attempts.append(Attempt(None, "no ```sql block in reply", "format"))
                messages.append({"role": "user", "content":
                                 "Reply with the query in a ```sql fenced block."})
                continue

            out = guard.run(sql)
            res.attempts.append(Attempt(out["sql"], out.get("error"),
                                        out.get("error_kind"), out.get("tables", [])))
            if "error" not in out:
                res.status, res.sql, res.assumptions, res.result = (
                    "ok", out["sql"], assumptions, out)
                break
            messages.append({"role": "user",
                             "content": _repair_prompt(out, sql, routing)})

        if res.status == "ok" and answer:
            msg = _call(client, model, ANSWER_SYSTEM, [{"role": "user", "content":
                        _answer_prompt(question, res)}])
            usage.add(msg)
            res.answer = _text(msg).strip()

        res.input_tokens, res.output_tokens = usage.inp, usage.out
        record_usage(sp, usage.inp, usage.out, model)
        set_io(sp, output={"status": res.status, "sql": res.sql,
                           "tables": routing.names,
                           "row_count": res.result.get("row_count")})
    return res


def _call(client: Anthropic, model: str, system: str,
          messages: list[dict[str, Any]]) -> Any:
    # client.beta because REQUEST_KWARGS may carry a beta flag (the refusal
    # fallback); with a third-party gateway configured those keys are already
    # stripped and this is a plain Messages call.
    msg = client.beta.messages.create(model=model, system=system,
                                      messages=messages, **REQUEST_KWARGS)
    if msg.stop_reason == "refusal":
        raise RuntimeError("the model declined this request (stop_reason=refusal)")
    return msg


def _text(msg: Any) -> str:
    return "\n".join(b.text for b in msg.content if b.type == "text")


_SQL_BLOCK = re.compile(r"```sql\s*(.+?)```", re.S | re.I)


def _parse(text: str) -> tuple[str | None, str | None, str | None]:
    """(sql, assumptions, cannot_answer_reason) from a generation reply."""
    m = re.search(r"CANNOT_ANSWER:\s*(.+)", text)
    blocks = _SQL_BLOCK.findall(text)
    if m and not blocks:
        return None, None, m.group(1).strip()
    a = re.search(r"Assumptions:\s*(.+)", text)
    return ((blocks[-1].strip() if blocks else None),
            (a.group(1).strip() if a else None), None)


def _generation_prompt(routing: Routing, question: str) -> str:
    return (f"Schema ({len(routing.tables)} of {routing.total_tables} tables, "
            f"selected for this question):\n\n{routing.schema_text()}\n\n"
            f"Question: {question}")


_QUALIFIED = re.compile(r"\b(ops|scm|qms|act)\.(\w+)", re.I)


def _repair_prompt(out: dict[str, Any], sql: str, routing: Routing) -> str:
    msg = [f"That query failed.\nError: {out['error']}"]
    # The model referenced a real table it was never shown: the router missed
    # it. Show it now rather than letting the model guess its columns.
    shown = set(routing.names)
    known = set(all_tables())
    used = {f"{s.lower()}.{t.lower()}" for s, t in _QUALIFIED.findall(sql)}
    missing = sorted((used & known) - shown)
    if missing:
        add = [RoutedTable(r["table"], r["ddl"], [], "requested")
               for r in describe(missing)["rows"]]
        routing.tables.extend(add)
        msg.append("You used tables that were not in the schema. Here they are:\n\n"
                   + "\n\n".join(t.ddl for t in add))
    msg.append("Fix the query under the same rules and reply in the same format.")
    return "\n\n".join(msg)


def _answer_prompt(question: str, res: Text2SQLResult) -> str:
    rows = res.result.get("rows", [])
    shown = rows[:ANSWER_ROWS]
    meta = {"row_count_returned": res.result.get("row_count"),
            "truncated": res.result.get("truncated", False),
            "rows_shown_below": len(shown)}
    return (f"Question: {question}\n\nSQL that was run:\n{res.sql}\n\n"
            f"Assumptions the query made: {res.assumptions or 'none stated'}\n\n"
            f"Result metadata: {json.dumps(meta)}\n\n"
            f"Rows:\n{json.dumps(shown, default=str)}")
