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

from proof.tools._base import as_text  # noqa: E402


def build(name: str, instructions: str) -> MCPServer:
    """Create a server with the house instructions attached.

    `instructions` reaches the model as server-level context. Keeping it here
    rather than in each agent's system prompt means the guidance travels WITH
    the tools -- if the production server is later consumed by something other
    than this project, the caveats come along.
    """
    return MCPServer(name=name, instructions=instructions)


def text_result(result: dict) -> str:
    """Every tool returns JSON text: rows, row_count, the SQL, and any note."""
    return as_text(result)
