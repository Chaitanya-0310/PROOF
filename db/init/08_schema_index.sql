-- =====================================================================
-- SCHEMA INDEX -- the table router behind text-to-SQL.
--
-- Every other question path in PROOF runs SQL a person wrote. Text-to-SQL
-- lets the model write the query for questions no tool anticipated. Handing
-- it the whole catalogue on every question is what this table avoids: ~20
-- tables of DDL is enough to bury the three that matter, and the model
-- starts joining tables because they are there.
--
-- So each queryable table gets one row here: its enriched DDL (types, keys,
-- enum values, sample codes, a business description) plus a lexical and a
-- dense representation of it. A question retrieves the few tables it needs
-- and only their DDL goes into the prompt.
--
-- Same engine and same hybrid recipe as docs.chunks (04_docs.sql), for the
-- same reasons: one system to run, and exact-term matching ('TOR1',
-- 'MATERIAL_OUT', 'PO-200001') where embeddings are weakest.
--
-- Idempotent on purpose: `make sql-migrate` replays this file against an
-- existing database, since db/init only runs on an empty volume.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.schema_index (
    table_name   text PRIMARY KEY,            -- 'ops.downtime_events'
    schema_name  text NOT NULL,
    description  text NOT NULL,
    -- What the generator sees. Enriched DDL, not raw pg_dump output: the
    -- CHECK constraint becomes "one of: ...", an FK becomes "-> ops.lines",
    -- and the business meaning of tricky columns sits next to them.
    ddl          text NOT NULL,
    -- What retrieval matches against: description, synonyms, column names
    -- and sample values. Kept separate from `ddl` so search vocabulary can
    -- grow without bloating the prompt.
    search_text  text NOT NULL,
    -- FK-adjacent tables, both directions. The router walks these to add
    -- the bridge tables a join needs but the question never mentions.
    neighbours   text[] NOT NULL DEFAULT '{}',
    fk_edges     jsonb  NOT NULL DEFAULT '[]',
    row_count    bigint,
    -- Pinned tables go into every prompt regardless of retrieval
    -- (ops.sim_clock: every time comparison needs "now").
    pinned       boolean NOT NULL DEFAULT false,
    tsv          tsvector GENERATED ALWAYS AS (
                     setweight(to_tsvector('english', translate(table_name, '._', '  ')), 'A')
                     || setweight(to_tsvector('english', search_text), 'B')
                 ) STORED,
    embedding    vector(384)                  -- all-MiniLM-L6-v2, as docs.chunks
);

CREATE INDEX IF NOT EXISTS schema_index_tsv_idx ON meta.schema_index USING gin (tsv);

-- Grants. Roles already exist by the time this runs (03, 07). The indexer
-- writes as seeder; the router reads as agent_read, which can still write
-- nothing anywhere.
GRANT USAGE ON SCHEMA meta TO seeder, agent_read;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA meta TO seeder;
GRANT SELECT ON ALL TABLES IN SCHEMA meta TO agent_read;
