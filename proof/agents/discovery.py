"""Tool discovery and the reliability rules around every MCP call.

What happens once per question, per domain:

1. **Start and discover, with a deadline.** Spawn the server, run the MCP
   handshake, list its tools. The handshake returns the server's
   `instructions`, which are kept and given to that domain's sub-agent -- they
   used to be thrown away here, so caveats written into a server (the
   analytics server's SQL rules, the action server's "never say done") never
   reached a model. The tool list is fetched ONCE and reused by every
   delegation, rather than re-listed per sub-agent turn.

2. **Fail one domain, not the question.** A server that will not start, or
   hangs in its handshake, is retried once and then marked unavailable. The
   coordinator is told -- in its roster and in that agent's tool result --
   and answers with what the other domains can give, naming what could not
   be determined. Before this, one broken server failed every question,
   including the ones that never needed it.

3. **Validate what was discovered.** Zero tools or a duplicated tool name
   makes the domain unavailable; a tool whose schema is malformed is
   dropped with a warning. A server that silently advertises nothing usable
   is otherwise indistinguishable from a sub-agent that "could not find" an
   answer.

What happens on every tool call (ReliableSession):

- **A timeout on every call.** A hung tool returns an error result the model
  can read, instead of holding the question open until the budget trips.
- **Retry only what is declared safe to repeat.** The server says so in the
  MCP tool annotations (read_only_hint / idempotent_hint, set in
  mcp_servers/_serve.py). The client never infers it from a tool's name. A
  tool with no annotation gets exactly one attempt.
- **A dead server is latched.** Once the connection has closed, later calls
  to that domain fail immediately with the reason, rather than each waiting
  out its own timeout.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT, CallToolResult, TextContent

from proof.identity import Principal

from .servers import ROOT, ServerSpec


def _env_float(name: str, default: float) -> float:
    # `or`, not a getenv default: a blank line copied from .env.example means
    # "unset", and float("") would crash at import.
    return float(os.getenv(name) or default)


# Handshake + list_tools. Generous: the first start on a cold machine imports
# torch in the quality and analytics servers.
STARTUP_TIMEOUT_S = _env_float("PROOF_MCP_STARTUP_TIMEOUT_S", 45)
STARTUP_ATTEMPTS = 2
# Per tool call. The guarded SQL path has its own 5 s statement timeout; this
# bounds everything else (embedding load on a first search, a lock wait).
TOOL_TIMEOUT_S = _env_float("PROOF_TOOL_TIMEOUT_S", 60)
RETRY_SAFE_ATTEMPTS = 2


class DiscoveryError(RuntimeError):
    """The server started but advertised nothing usable."""


@dataclass
class DomainSession:
    """One domain's server, as discovered at the start of a question."""
    spec: ServerSpec
    session: "ReliableSession | None"
    tools: list[Any] = field(default_factory=list)       # mcp.types.Tool
    instructions: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.session is not None and self.session.dead is None

    @property
    def unavailable_reason(self) -> str:
        if self.session is None:
            return self.error or "server did not start"
        return f"server stopped mid-question: {self.session.dead}"

    def retry_safe(self) -> set[str]:
        return retry_safe_tools(self.tools)


def retry_safe_tools(tools: list[Any]) -> set[str]:
    """Tools whose annotations declare a repeated call has no extra effect."""
    safe = set()
    for t in tools:
        a = getattr(t, "annotations", None)
        if a is not None and (a.read_only_hint or a.idempotent_hint):
            safe.add(t.name)
    return safe


async def open_domain(stack: AsyncExitStack, spec: ServerSpec,
                      principal: Principal) -> DomainSession:
    """Start one server and discover its tools; never raises for a bad server.

    Each attempt gets its own exit stack, so a failed attempt's subprocess
    and streams are torn down before the retry, and only a healthy session is
    handed to the question's stack. The identity is injected into the
    subprocess environment here, once, by the authenticated client -- it
    never travels through the model.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_servers" / spec.script)],
        cwd=str(ROOT / "mcp_servers"),
        env=spec.env(principal),
    )
    failures: list[str] = []
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        attempt_stack = AsyncExitStack()
        try:
            # Spawning is entered OUTSIDE the deadline: these contexts open
            # anyio task groups that stay open for the whole question, and a
            # cancel scope (fail_after) must not close while a task group
            # opened inside it is still running. Only the handshake, where a
            # broken server actually hangs, is under the deadline.
            read, write = await attempt_stack.enter_async_context(stdio_client(params))
            session = await attempt_stack.enter_async_context(ClientSession(read, write))
            with anyio.fail_after(STARTUP_TIMEOUT_S):
                init = await session.initialize()
                tools = (await session.list_tools()).tools
            tools, warnings = validate_tools(tools)
        except Exception as exc:  # noqa: BLE001 - recorded, and the domain is marked down
            failures.append(f"attempt {attempt}: {_describe(exc)}")
            cleanup = await _close(attempt_stack)
            if cleanup:
                failures.append(f"attempt {attempt} cleanup: {cleanup}")
            continue
        stack.push_async_callback(_close, attempt_stack)
        return DomainSession(
            spec=spec,
            session=ReliableSession(session, spec.domain, retry_safe_tools(tools)),
            tools=tools,
            instructions=(init.instructions or "").strip() or None,
            warnings=warnings,
        )
    return DomainSession(spec=spec, session=None,
                         error=f"{spec.server_name} failed to start: " + "; ".join(failures))


def validate_tools(tools: list[Any]) -> tuple[list[Any], list[str]]:
    """Drop malformed tools; refuse a tool list nothing can be done with."""
    if not tools:
        raise DiscoveryError("server advertised no tools")
    names = [t.name for t in tools]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        # Which one the model gets would depend on list order. Refuse.
        raise DiscoveryError(f"duplicate tool names: {', '.join(dupes)}")
    kept, warnings = [], []
    for t in tools:
        schema = t.input_schema or {}
        props = schema.get("properties") or {}
        missing = [r for r in schema.get("required") or [] if r not in props]
        if schema.get("type") != "object" or missing:
            warnings.append(f"dropped {t.name}: malformed input schema"
                            + (f" (required but undefined: {missing})" if missing else ""))
            continue
        if not (t.description or "").strip():
            # Kept, but flagged: a tool with no description is chosen blind.
            warnings.append(f"{t.name} has no description")
        kept.append(t)
    if not kept:
        raise DiscoveryError("every advertised tool was malformed: " + "; ".join(warnings))
    return kept, warnings


class ReliableSession:
    """call_tool with a deadline, annotation-driven retry, and a dead-server latch.

    Sits under TimedSession, so the timeline and trace show one tool call
    covering its retries -- which is what the user waited for.
    """

    def __init__(self, session: ClientSession, domain: str, retry_safe: set[str]) -> None:
        self._session = session
        self._domain = domain
        self._retry_safe = retry_safe
        self.dead: str | None = None
        self.retries = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def call_tool(self, name: str, arguments: Any = None, *args: Any, **kwargs: Any):
        if self.dead is not None:
            return error_result(f"the {self._domain} server is no longer running "
                                f"({self.dead}); this call was not made", retryable=False)
        kwargs.setdefault("read_timeout_seconds", TOOL_TIMEOUT_S)
        safe = name in self._retry_safe
        attempts = RETRY_SAFE_ATTEMPTS if safe else 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._session.call_tool(name, arguments, *args, **kwargs)
            except MCPError as exc:
                if exc.code == CONNECTION_CLOSED:
                    self.dead = exc.message or "connection closed"
                    return error_result(
                        f"the {self._domain} server stopped during {name}"
                        + ("" if safe else " -- it may or may not have taken effect"),
                        retryable=False)
                if exc.code == REQUEST_TIMEOUT and attempt < attempts:
                    self.retries += 1
                    continue
                if exc.code == REQUEST_TIMEOUT:
                    return error_result(
                        f"{name} timed out after {kwargs['read_timeout_seconds']:.0f}s"
                        + (f" ({attempts} attempts)" if attempts > 1 else
                           " -- it may or may not have taken effect"),
                        retryable=False)
                # A protocol-level refusal (bad arguments, unknown tool). The
                # model can act on the message; repeating the call cannot.
                return error_result(f"{name} failed: {exc.message}", retryable=False)
        raise AssertionError("unreachable")  # loop always returns


def error_result(message: str, *, retryable: bool) -> CallToolResult:
    """An is_error tool result in the same JSON shape as every tool's output.

    So the sub-agent's evidence parser reads it like any other result, and
    the model sees the failure as data it must report rather than as silence.
    """
    payload = {"error": message, "retryable": retryable, "rows": [], "row_count": 0,
               "sql": None,
               "note": "Tool failure, not an empty result. Say this could not be "
                       "determined; do not substitute an estimate."}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                          is_error=True)


def _describe(exc: BaseException) -> str:
    """One line for an exception, including what an ExceptionGroup wraps."""
    if isinstance(exc, TimeoutError):
        return f"no handshake within {STARTUP_TIMEOUT_S:.0f}s"
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


async def _close(stack: AsyncExitStack) -> str | None:
    """Tear down one server's contexts. Returns the error instead of raising.

    A server that crashed often fails again on the way out; that must not
    mask the startup error being reported, nor stop the other servers'
    teardown at the end of the question.
    """
    try:
        await stack.aclose()
    except Exception as exc:  # noqa: BLE001 - returned to the caller, not dropped
        return _describe(exc)
    return None
