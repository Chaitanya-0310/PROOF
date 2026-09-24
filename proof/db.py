"""Shared connection helpers. Every component connects as its OWN role.

The role a component gets is the security boundary. `agent_read` physically
cannot write; that is enforced by Postgres grants, not by prompt wording.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

# Role -> env var holding that role's password.
_ROLE_PW_ENV = {
    "seeder": "SEEDER_PASSWORD",
    "agent_read": "AGENT_READ_PASSWORD",
    "action_rw": "ACTION_RW_PASSWORD",
    # Phase 4. Used ONLY by the human approval CLI, never by an agent -- it
    # is the one role holding UPDATE on act.proposed_actions.
    "approver": "APPROVER_PASSWORD",
}


def load_env() -> None:
    """Load .env when running on the host (outside docker compose).

    Inside the app container the vars are already in the environment via
    env_file, so this is effectively a no-op there.

    Call this EARLY -- before reading any configuration, not just before
    connecting. This used to be private and called only from connect(), which
    meant anything checking os.getenv("ANTHROPIC_API_KEY") before the first
    database call saw nothing, and a key sitting correctly in .env was
    reported as missing.

    setdefault, not overwrite: a real environment variable always wins over
    the file, which is what a deployment expects.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def connect(role: str, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection authenticated as `role`."""
    if role not in _ROLE_PW_ENV:
        raise ValueError(f"unknown role {role!r}; expected one of {list(_ROLE_PW_ENV)}")
    load_env()
    return psycopg.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5434"),
        dbname=os.getenv("PGDATABASE", "proof"),
        user=role,
        password=os.environ[_ROLE_PW_ENV[role]],
        autocommit=autocommit,
    )


def sim_now(conn: psycopg.Connection):
    """The simulation's 'current time'.

    Nothing in this project calls now() directly. Demos must replay
    identically on any machine on any day, so 'now' is a value in the
    database that the seeder set, not a property of the wall clock.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT now_ts FROM ops.sim_clock WHERE id = 1")
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("sim_clock is empty -- run `make seed` first")
    return row[0]
