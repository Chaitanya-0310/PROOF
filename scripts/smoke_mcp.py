"""
Start each MCP server as a real subprocess and exercise it over the protocol.

This is the layer between "the SQL is right" (smoke_tools.py) and "the agent
chose correctly" (Phase 5 evals). It proves the four servers actually speak
MCP: that they advertise the tools we think they do, with the schemas we think
they have, and that a round-trip call returns real plant data.

No API key needed -- this is the protocol, not the model.

Run:  make smoke-mcp
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

# NOTE: mcp 2.x renamed the protocol model fields to snake_case
# (input_schema, is_error). The 1.x camelCase spellings raise
# AttributeError, and inside a TaskGroup that surfaces as an opaque
# ExceptionGroup rather than the real error -- hence the unwrapping in
# the probe loop below.

from proof.agents.servers import SERVERS  # noqa: E402
from proof.identity import resolve  # noqa: E402

# Every server is spawned with a session identity, exactly as the
# coordinator spawns it. The actions server refuses to start without
# one, which is the behaviour we want to exercise here.
SESSION = resolve("a.morin")

# One representative call per server, chosen to hit the tool that domain
# exists for rather than the easiest one.
PROBES = {
    "production": ("get_open_downtime", {}),
    "inventory": ("project_wip_expiry", {"line_id": 3, "restart_in_minutes": 90}),
    "demand": ("get_orders_for_run", {"run_id": 923}),
    "actions": ("whoami", {}),
    "quality": ("get_sku_allergens", {"category": "sweet_goods"}),
}


async def probe(domain: str) -> tuple[bool, str]:
    spec = SERVERS[domain]
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_servers" / spec.script)],
        cwd=str(ROOT / "mcp_servers"),
        env=spec.env(SESSION),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = (await session.list_tools()).tools
            names = [t.name for t in tools]

            print(f"\n  {spec.server_name}  ({len(tools)} tools)")
            for t in tools:
                required = t.input_schema.get("required", [])
                props = list(t.input_schema.get("properties", {}))
                opt = [p for p in props if p not in required]
                sig = ", ".join(required + [f"{p}?" for p in opt])
                print(f"    - {t.name}({sig})")

            if init.instructions:
                first = init.instructions.split(".")[0]
                print(f"    instructions: {first}...")

            tool_name, args = PROBES[domain]
            if tool_name not in names:
                return False, f"probe tool {tool_name} not advertised"

            res = await session.call_tool(tool_name, args)
            if res.is_error:
                return False, f"{tool_name} returned an error: {res.content}"

            payload = json.loads(res.content[0].text)
            print(f"    probe {tool_name} -> {payload['row_count']} row(s)")
            if payload["row_count"] == 0:
                return False, f"{tool_name} returned no rows"
            # Every tool must account for its provenance. Most return the SQL
            # they ran; a few (whoami) legitimately run none, and those must
            # say so with a note rather than returning a bare empty string.
            # Accepting a silent empty `sql` would let a real regression in
            # the lineage plumbing pass unnoticed.
            if "sql" not in payload:
                return False, f"{tool_name} omitted the sql field entirely"
            if not payload["sql"] and not payload.get("note"):
                return False, (f"{tool_name} returned no SQL and no note "
                               f"explaining why")

            sample = payload["rows"][0]
            keys = list(sample)[:5]
            print(f"    first row: " +
                  ", ".join(f"{k}={sample[k]!r}" for k in keys))
            return True, f"{len(tools)} tools, probe ok"


async def main() -> int:
    print("Starting each MCP server as a subprocess and calling it over stdio")
    results = []
    for domain in SERVERS:
        try:
            ok, detail = await probe(domain)
        except Exception as exc:  # noqa: BLE001 - report, don't mask
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((ok, domain, detail))

    print(f"\n{'=' * 68}\nresults\n{'=' * 68}")
    for ok, domain, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {domain:12s} {detail}")
    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} servers healthy")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
