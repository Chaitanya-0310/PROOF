#!/bin/bash
# Grants for the Phase 4 action queue.
#
# THIS FILE IS THE AUTHORIZATION MODEL. Read it before reading any Python.
#
#   agent_read  -- may SEE the queue (it needs to tell the user what is
#                  pending) but holds NO write grant on it. The read path
#                  cannot create a proposal.
#   action_rw   -- may INSERT proposals and append to the decision log.
#                  It may NOT update status, so it cannot approve. It has no
#                  write grant anywhere in ops/scm/qms, so an approved
#                  reallocation cannot be applied by the agent either.
#   approver    -- a separate login used only by the human approval CLI. It
#                  is the ONLY role that may set status.
#
# The separation means "the agent approved its own proposal" is not a bug
# that a prompt change can introduce. It would require a new database grant.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE ROLE approver LOGIN PASSWORD '${APPROVER_PASSWORD}';

    GRANT USAGE ON SCHEMA act TO seeder, agent_read, action_rw, approver;

    -- seeder: loads the demo principals.
    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
        ON ALL TABLES IN SCHEMA act TO seeder;
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA act TO seeder;

    -- agent_read: read the queue, write nothing.
    GRANT SELECT ON ALL TABLES IN SCHEMA act TO agent_read;

    -- action_rw: propose only. Note the absence of UPDATE on
    -- proposed_actions -- that omission is what makes approval a human step.
    GRANT SELECT ON ALL TABLES IN SCHEMA act TO action_rw;
    GRANT INSERT ON act.proposed_actions TO action_rw;
    GRANT INSERT ON act.decision_log     TO action_rw;
    GRANT INSERT ON act.authz_denials    TO action_rw;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA act TO action_rw;

    -- approver: the human CLI. The only role that may decide or execute.
    GRANT SELECT ON ALL TABLES IN SCHEMA act TO approver;
    GRANT UPDATE ON act.proposed_actions TO approver;
    GRANT INSERT ON act.decision_log     TO approver;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA act TO approver;
    -- The approver also needs to read the plant to render an approval screen.
    GRANT USAGE ON SCHEMA ops, scm, qms TO approver;
    GRANT SELECT ON ALL TABLES IN SCHEMA ops, scm, qms TO approver;
SQL

echo "action queue grants applied (approver role created)"
