"""The five MCP servers, and how to launch them.

One place that knows the server topology, so the smoke test, the sub-agents
and the coordinator cannot drift apart about what exists.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from proof.identity import PRINCIPAL_ENV

ROOT = Path(__file__).resolve().parent.parent.parent

# Environment the server subprocesses need. Only the database connection --
# notably NOT the Anthropic key: an MCP server has no business holding model
# credentials, and a server that cannot call a model cannot be talked into
# calling one.
_DB_ENV_KEYS = ("PGHOST", "PGPORT", "PGDATABASE",
                "AGENT_READ_PASSWORD", "ACTION_RW_PASSWORD")


@dataclass(frozen=True)
class ServerSpec:
    domain: str
    script: str
    server_name: str
    # What this domain is FOR, in the words the coordinator sees when it is
    # deciding whom to ask. Written as a boundary ("owns X, does not know Y")
    # because routing errors come from overlap, not from missing detail.
    charter: str

    def env(self, principal=None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if k in _DB_ENV_KEYS or k in ("PATH", "SYSTEMROOT", "PYTHONPATH")}
        # Servers run with cwd=mcp_servers/ so `import _serve` resolves; the
        # repo root must still be importable for `proof.tools`.
        env["PYTHONPATH"] = str(ROOT)
        env.setdefault("PGHOST", os.getenv("PGHOST", "localhost"))
        env.setdefault("PGPORT", os.getenv("PGPORT", "5434"))
        env.setdefault("PGDATABASE", os.getenv("PGDATABASE", "proof"))

        # Serve embeddings strictly from the local cache.
        #
        # sentence-transformers otherwise makes HuggingFace metadata calls on
        # every process start. Nothing confidential is in those calls, but
        # "the embedding model runs locally and nothing leaves the machine"
        # should be exactly true rather than nearly true -- a food
        # manufacturer's security review will ask, and "only metadata" is a
        # worse answer than "no egress". It also removes a network dependency
        # from the startup path of a plant-floor tool.
        #
        # Safe because `make index` must run before the server is useful, and
        # indexing populates the cache.
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        # Cache location must be inherited or the offline flag finds nothing.
        for key in ("HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE",
                    "USERPROFILE", "HOME", "LOCALAPPDATA"):
            if os.getenv(key):
                env[key] = os.environ[key]

        # THE SESSION IDENTITY, injected at spawn time.
        #
        # This is the whole identity design. The principal is resolved once,
        # by the authenticated client, and handed to the subprocess through
        # its environment. No tool accepts a principal argument, so the model
        # has nothing to forge -- "I am the regional director" is words in a
        # conversation the authorization layer never reads.
        if principal is not None:
            env[PRINCIPAL_ENV] = principal.to_env()
        return env


SERVERS: dict[str, ServerSpec] = {
    "production": ServerSpec(
        domain="production",
        script="production_server.py",
        server_name="proof-production",
        charter=(
            "Line state, downtime, output-loss arithmetic, alternate-line "
            "search, and scrap analytics. Owns MES/SCADA. Does NOT know about "
            "perishable WIP expiry, customer orders, or raw materials."
        ),
    ),
    "inventory": ServerSpec(
        domain="inventory",
        script="inventory_server.py",
        server_name="proof-inventory",
        charter=(
            "Perishable WIP on the floor (the proof/scrap clock) and raw "
            "material stock, purchase orders and runout projection. Owns "
            "WMS + WIP tracking. THE ONLY domain that knows whether staged "
            "dough survives a stoppage."
        ),
    ),
    "demand": ServerSpec(
        domain="demand",
        script="demand_server.py",
        server_name="proof-demand",
        charter=(
            "The customer order book: commitments, ship dates, shortfalls and "
            "customer priority tiers. Owns ERP order management. Does NOT know "
            "line state."
        ),
    ),
    "actions": ServerSpec(
        domain="actions",
        script="action_server.py",
        server_name="proof-actions",
        charter=(
            "The action queue -- the ONLY write path. Propose a reallocation, "
            "a customer notification, a PO expedite or a quality hold, and it "
            "is queued for a named human to approve. Nothing here changes the "
            "plant. Also answers 'what am I allowed to do' via whoami."
        ),
    ),
    "quality": ServerSpec(
        domain="quality",
        script="quality_server.py",
        server_name="proof-quality",
        charter=(
            "Quality holds and allergen compatibility rules. Owns the QMS. "
            "Allergen compatibility is a hard constraint on any line move. "
            "(Phase 3 adds SOP retrieval with mandatory citations.)"
        ),
    ),
}
