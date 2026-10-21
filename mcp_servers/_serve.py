"""Shared MCP server scaffolding.

Why MCP at all, when these are just Python functions in the same repo?

Because the agent should only carry the REASONING about which tool to use --
not the integration work of how to call each system, what its schema is, which
fields are mandatory, and how its errors behave. Bolting that into agent code
couples every agent to every system it touches.

Here each domain is a separate process speaking a standard protocol. The
production agent does not import `psycopg`; it speaks MCP to a server that
does. Swapping the production server's backend from this Postgres to a real
MES is then a change inside ONE process, with no agent code touched.

That is the whole argument, and it is also why the four servers are separate
processes rather than four namespaces in one: a real deployment would have
them owned by different teams, on different networks, with different uptime.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The servers are launched as subprocesses (`python mcp_servers/x_server.py`),
# so the repo root is not on sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from proof.tools._base import as_text  # noqa: E402

# MCP tool annotations: the server's declaration of what calling a tool does.
# The client reads them at discovery (proof/agents/discovery.py) and decides
# from them, not from tool names, which calls are safe to retry. A tool with
# no annotation is treated as unsafe to repeat.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)
# Writes that are safe to repeat: the proposal tools, which deduplicate on an
# idempotency key in the database (proof/tools/actions.py), so a retried
# call returns the action already queued instead of queueing another.
IDEMPOTENT_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                   idempotent_hint=True, open_world_hint=False)


def build(name: str, instructions: str, *, read_only: bool = True) -> MCPServer:
    """Create a server with the house instructions and tool annotations attached.

    `instructions` is returned in the MCP initialize handshake. The client
    side (proof/agents/discovery.py) keeps it and puts it in the sub-agent's
    system prompt, so the guidance travels WITH the tools -- if the
    production server is later consumed by something other than this
    project, the caveats come along.

    With `read_only` (every server but the action queue), each tool is
    annotated READ_ONLY unless its decorator says otherwise. The default is
    applied here so a new tool cannot forget it; a write server passes
    read_only=False and annotates each tool itself.
    """
    server = MCPServer(name=name, instructions=instructions)
    default = READ_ONLY if read_only else None
    original = server.tool

    def tool(fn=None, /, **kwargs):
        kwargs.setdefault("annotations", default)
        if callable(fn):             # bare @server.tool
            return original(**kwargs)(fn)
        return original(fn, **kwargs)

    server.tool = tool
    return server


def text_result(result: dict) -> str:
    """Every tool returns JSON text: rows, row_count, the SQL, and any note."""
    return as_text(result)
