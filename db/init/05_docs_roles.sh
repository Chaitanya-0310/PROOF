#!/bin/bash
# Grants for the Phase 3 document store.
#
# Separate from 03_roles.sh because init scripts run in filename order and
# docs.* does not exist until 04. Granting on a schema that is not there yet
# fails the whole bootstrap.
#
# Same posture as the plant schemas: the agent reads, and nothing more. A
# retrieval agent that could write to the SOP store would be a way to edit
# food-safety procedure by prompt.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    GRANT USAGE ON SCHEMA docs TO seeder, agent_read, action_rw;

    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
        ON ALL TABLES IN SCHEMA docs TO seeder;
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA docs TO seeder;

    GRANT SELECT ON ALL TABLES IN SCHEMA docs TO agent_read, action_rw;
    ALTER DEFAULT PRIVILEGES IN SCHEMA docs
        GRANT SELECT ON TABLES TO agent_read, action_rw;
SQL

echo "docs schema grants applied"
