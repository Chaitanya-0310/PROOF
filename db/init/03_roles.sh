#!/bin/bash
# Least-privilege login roles, created once on first boot as superuser.
#
# The point: by Phase 4 the agent's read path and its write path are
# DIFFERENT database principals. An agent that has been prompt-injected
# still cannot write to a plant table, because the connection it holds
# has no such grant. Authorization that lives in the database cannot be
# talked out of by a clever prompt -- that is the whole argument.
#
#   seeder      -> writes synthetic plant data (Phase 1 only)
#   agent_read  -> READ ONLY on ops/scm/qms; used by the MCP servers
#   action_rw   -> reserved for Phase 4; will write ONLY to the action queue
set -euo pipefail

# CAREFUL: the heredoc delimiter below is UNQUOTED, because the password
# variables must expand. That means the shell also expands backticks and
# dollar-brace inside the SQL body -- including inside SQL comments. Two
# real bugs came from this: a backticked word ran as a command, and a
# dollar-brace reference tripped 'set -u' with an unbound variable. Keep
# prose out of the SQL body; put it in shell comments like this one.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE ROLE seeder     LOGIN PASSWORD '${SEEDER_PASSWORD}';
    CREATE ROLE agent_read LOGIN PASSWORD '${AGENT_READ_PASSWORD}';
    CREATE ROLE action_rw  LOGIN PASSWORD '${ACTION_RW_PASSWORD}';

    -- ============================================================
    -- seeder: full write on the three plant schemas. Phase 1 only.
    -- In a real deployment this role would not exist; the data would
    -- arrive from MES/ERP replication.
    -- ============================================================
    GRANT USAGE ON SCHEMA ops, scm, qms TO seeder;
    -- TRUNCATE is a distinct privilege in Postgres, not implied by DELETE.
    -- The seeder re-truncates on every run so re-seeding is idempotent.
    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
        ON ALL TABLES IN SCHEMA ops, scm, qms TO seeder;
    -- UPDATE on sequences is what setval() needs. The seeder assigns primary
    -- keys explicitly (for determinism) and then fast-forwards the sequences,
    -- rather than owning them outright.
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA ops, scm, qms TO seeder;

    -- ============================================================
    -- agent_read: SELECT and nothing else, forever.
    -- Note there is no INSERT/UPDATE/DELETE anywhere in this block.
    -- That omission is the security control.
    -- ============================================================
    GRANT USAGE ON SCHEMA ops, scm, qms TO agent_read;
    GRANT SELECT ON ALL TABLES IN SCHEMA ops, scm, qms TO agent_read;
    -- Tables added by later phases inherit the same read-only posture.
    ALTER DEFAULT PRIVILEGES IN SCHEMA ops, scm, qms
        GRANT SELECT ON TABLES TO agent_read;

    -- ============================================================
    -- action_rw: can SEE the plant (it needs context to draft an
    -- action) but its write grants arrive in Phase 4, scoped to the
    -- action-queue schema only.
    -- ============================================================
    GRANT USAGE ON SCHEMA ops, scm, qms TO action_rw;
    GRANT SELECT ON ALL TABLES IN SCHEMA ops, scm, qms TO action_rw;
SQL

echo "roles created: seeder, agent_read, action_rw"
