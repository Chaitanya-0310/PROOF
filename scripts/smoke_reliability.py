"""
Idempotency and reliability: the write path, discovery, and every tool call.
No API key needed.

1. **Proposals are idempotent.** The same request twice is one pending
   action; rewording it does not make it new; a different request, a
   different proposer, or an expired original does. Eight concurrent
   identical proposals produce exactly one row -- the case a check-then-
   insert without a lock gets wrong.

2. **Discovery keeps what the servers declare.** Every server's handshake
   instructions and tool annotations are captured, and the retry policy that
   follows from the annotations is the one intended: every read tool and
   every proposal is retry-safe, nothing else is.

3. **A broken server fails its own domain, not the question.** A server
   that crashes on start and one that never answers the handshake both come
   back as an unavailable DomainSession, after a bounded retry, without
   raising.

4. **Tool calls time out, retry only when declared safe, and latch a dead
   server**, exercised against a scripted fake session so each failure mode
   is deterministic.

Run:  make smoke-reliability
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp.shared.exceptions import MCPError  # noqa: E402
from mcp.types import CONNECTION_CLOSED, INVALID_PARAMS, REQUEST_TIMEOUT  # noqa: E402

from proof.agents import discovery  # noqa: E402
from proof.agents.servers import SERVERS  # noqa: E402
from proof.agents.subagent import SUBAGENT_SYSTEM, _server_guidance  # noqa: E402
from proof.db import connect, load_env  # noqa: E402
from proof.identity import resolve  # noqa: E402
from proof.tools import actions  # noqa: E402

load_env()

failures: list[str] = []


def ok(cond: bool, label: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


# A run id no real proposal uses, so the test rows are unmistakable and the
# cleanup cannot touch a demo proposal.
TEST_RUN = 990001


def _propose(who: str, to_line: str = "L5", units: int = 1800,
             rationale: str = "smoke_reliability") -> dict:
    return actions.propose_reallocate_run(
        resolve(who), "TOR1", TEST_RUN, "L3", to_line, units, 25, rationale,
        source_question="smoke_reliability")["rows"][0]


def _cleanup() -> None:
    with connect("seeder") as conn, conn.cursor() as cur:
        cur.execute("""DELETE FROM act.decision_log WHERE action_id IN
                       (SELECT action_id FROM act.proposed_actions
                        WHERE source_question = 'smoke_reliability')""")
        cur.execute("DELETE FROM act.proposed_actions WHERE source_question = 'smoke_reliability'")
        conn.commit()


def _count(sql: str, *params) -> int:
    with connect("seeder") as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def test_idempotent_proposals() -> None:
    print("\n1. Proposals are idempotent")
    _cleanup()
    try:
        first = _propose("a.morin")
        again = _propose("a.morin")
        ok(again["action_id"] == first["action_id"] and again["deduplicated"]
           and not first["deduplicated"],
           f"same request twice -> one action ({first['action_id']}), second flagged deduplicated")

        reworded = _propose("a.morin", units=2100, rationale="re-estimated, same move")
        ok(reworded["action_id"] == first["action_id"],
           "reworded rationale and revised units_at_risk are still the same request")

        logged = _count("SELECT count(*) FROM act.decision_log WHERE action_id = %s "
                        "AND event = 'proposed'", first["action_id"])
        ok(logged == 1, f"decision log records the proposal once, not per call ({logged})")

        other_line = _propose("a.morin", to_line="L4")
        ok(other_line["action_id"] != first["action_id"], "a different target line is a new request")
        other_person = _propose("j.okafor")
        ok(other_person["action_id"] != first["action_id"],
           "a different proposer is a new request (self-approval depends on who proposed)")

        # Expire the original the way the clock would. Only the seeder may
        # write this column; the agent's role cannot, which is the point.
        with connect("seeder") as conn, conn.cursor() as cur:
            cur.execute("UPDATE act.proposed_actions SET expires_at = proposed_at - interval '1 minute' "
                        "WHERE action_id = %s", (first["action_id"],))
            conn.commit()
        revived = _propose("a.morin")
        ok(revived["action_id"] != first["action_id"] and not revived["deduplicated"],
           "an expired pending proposal does not block proposing it again")

        _cleanup()
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: _propose("a.morin")["action_id"], range(8)))
        rows = _count("SELECT count(*) FROM act.proposed_actions "
                      "WHERE source_question = 'smoke_reliability'")
        ok(len(set(ids)) == 1 and rows == 1,
           f"8 concurrent identical proposals -> {rows} row(s), {len(set(ids))} distinct id(s)")
    finally:
        _cleanup()


async def test_discovery() -> None:
    print("\n2. Discovery: instructions and annotations survive the handshake")
    principal = resolve("a.morin")
    async with AsyncExitStack() as stack:
        domains = {n: await discovery.open_domain(stack, s, principal)
                   for n, s in SERVERS.items()}
        for name, d in domains.items():
            ok(d.available and d.tools and d.instructions,
               f"{name}: {len(d.tools)} tools, {len(d.instructions or '')} chars of instructions"
               + (f", warnings: {d.warnings}" if d.warnings else ""))

        for name, d in domains.items():
            if name == "actions":
                continue
            ok(d.retry_safe() == {t.name for t in d.tools},
               f"{name}: every tool annotated read-only, so retry-safe")
        act = {t.name: t.annotations for t in domains["actions"].tools}
        proposes = {n for n in act if n.startswith("propose_")}
        ok(all(act[n].idempotent_hint and not act[n].read_only_hint for n in proposes),
           f"actions: {len(proposes)} propose_* tools declared idempotent writes")
        ok(domains["actions"].retry_safe() == set(act),
           "actions: every tool retry-safe now that proposals deduplicate")

        g = _server_guidance(domains["analytics"].spec, domains["analytics"].instructions)
        prompt = SUBAGENT_SYSTEM.format(domain="analytics", charter="x", server_guidance=g)
        ok("sim_clock" in prompt and "proof-analytics" in prompt,
           "server instructions reach the sub-agent's system prompt")

        # One live call through the reliable wrapper, end to end.
        res = await domains["production"].session.call_tool("get_open_downtime", {})
        ok(not res.is_error, "a real call through ReliableSession succeeds")


async def test_broken_servers() -> None:
    print("\n3. A broken server takes down its domain, not the question")
    principal = resolve("a.morin")
    tmp = Path(tempfile.mkdtemp(prefix="proof_smoke_"))
    crash = tmp / "crash_server.py"
    crash.write_text("raise SystemExit('boom: misconfigured server')\n")
    hang = tmp / "hang_server.py"
    hang.write_text(textwrap.dedent("""
        import time
        while True:
            time.sleep(1)   # never speaks MCP
    """))
    base = SERVERS["demand"]
    saved = discovery.STARTUP_TIMEOUT_S
    discovery.STARTUP_TIMEOUT_S = 2
    try:
        for label, script in (("crashes on start", crash), ("never answers the handshake", hang)):
            spec = replace(base, domain=f"broken_{script.stem}", script=str(script),
                           server_name=f"proof-{script.stem}")
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            async with AsyncExitStack() as stack:
                d = await discovery.open_domain(stack, spec, principal)
            took = loop.time() - t0
            ok(not d.available and "attempt 2" in (d.error or ""),
               f"{label}: unavailable after {discovery.STARTUP_ATTEMPTS} attempts "
               f"in {took:.1f}s -- {(d.error or '')[:80]}")
    finally:
        discovery.STARTUP_TIMEOUT_S = saved

    try:
        discovery.validate_tools([SimpleNamespace(name="a", description="", input_schema={}),
                                  SimpleNamespace(name="a", description="", input_schema={})])
        ok(False, "duplicate tool names refused")
    except discovery.DiscoveryError:
        ok(True, "duplicate tool names refused")
    kept, warns = discovery.validate_tools([
        SimpleNamespace(name="good", description="d",
                        input_schema={"type": "object", "properties": {"x": {}}, "required": ["x"]}),
        SimpleNamespace(name="bad", description="d",
                        input_schema={"type": "object", "properties": {}, "required": ["y"]})])
    ok([t.name for t in kept] == ["good"] and any("bad" in w for w in warns),
       "a tool requiring an undefined argument is dropped with a warning")


class FakeSession:
    """Scripted outcomes per call: an exception to raise, or a result."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    async def call_tool(self, name, arguments=None, *args, **kwargs):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


async def test_call_policy() -> None:
    print("\n4. Tool calls: timeout, annotation-driven retry, dead-server latch")
    okres = SimpleNamespace(is_error=False)
    timeout = lambda: MCPError(code=REQUEST_TIMEOUT, message="timed out")  # noqa: E731

    fake = FakeSession([timeout(), okres])
    rs = discovery.ReliableSession(fake, "demo", retry_safe={"read_tool"})
    res = await rs.call_tool("read_tool", {})
    ok(res is okres and fake.calls == 2 and rs.retries == 1,
       "retry-safe tool: timeout, then retried once and succeeded")

    fake = FakeSession([timeout(), okres])
    rs = discovery.ReliableSession(fake, "demo", retry_safe=set())
    res = await rs.call_tool("unannotated_write", {})
    ok(res.is_error and fake.calls == 1 and "may or may not" in res.content[0].text,
       "unannotated tool: one attempt only, failure says the outcome is unknown")

    fake = FakeSession([MCPError(code=INVALID_PARAMS, message="bad args")])
    rs = discovery.ReliableSession(fake, "demo", retry_safe={"read_tool"})
    res = await rs.call_tool("read_tool", {})
    ok(res.is_error and fake.calls == 1, "a protocol refusal is reported, never retried")

    fake = FakeSession([MCPError(code=CONNECTION_CLOSED, message="Connection closed")])
    rs = discovery.ReliableSession(fake, "demo", retry_safe={"read_tool"})
    await rs.call_tool("read_tool", {})
    res = await rs.call_tool("read_tool", {})
    ok(rs.dead and res.is_error and fake.calls == 1,
       "a closed connection is latched: the next call fails fast without being sent")

    d = discovery.DomainSession(spec=SERVERS["demand"], session=rs)
    ok(not d.available and "stopped mid-question" in d.unavailable_reason,
       "the domain reports itself unavailable once its server has died")


def main() -> int:
    test_idempotent_proposals()
    asyncio.run(test_discovery())
    asyncio.run(test_broken_servers())
    asyncio.run(test_call_policy())
    print(f"\n{'=' * 60}")
    print("all passed" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
