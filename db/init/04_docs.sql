-- =====================================================================
-- DOCUMENT STORE -- the procedural half of quality.
--
-- Phase 2 put allergen rules in SQL because they are hard constraints that
-- must be checkable. This schema holds the other half: SOPs, work
-- instructions, HACCP plans and policies, which are prose and have to be
-- retrieved and quoted rather than computed.
--
-- Both halves live in one database on purpose. A retrieval query can then
-- filter procedures by the line they apply to with an ordinary join, instead
-- of the agent fetching a candidate list from a vector store and then
-- separately asking whether it is relevant to line 3.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS docs;

CREATE TABLE docs.documents (
    doc_id         text PRIMARY KEY,          -- 'SOP-RESTART-FLAT'
    title          text NOT NULL,
    doc_type       text NOT NULL CHECK (doc_type IN
                       ('sop', 'work_instruction', 'haccp', 'policy')),
    version        text NOT NULL,
    effective_date date NOT NULL,
    owner          text NOT NULL,
    -- Scoping. A restart procedure for flatbread must not be quoted at a
    -- sweet-goods line, and retrieval that cannot express that will
    -- eventually produce a confidently wrong food-safety answer.
    applies_to_categories text[] NOT NULL DEFAULT '{}',
    applies_to_lines      text[] NOT NULL DEFAULT '{}',  -- 'TOR1/L3'
    source_path    text NOT NULL,
    body           text NOT NULL
);

-- ---------------------------------------------------------------------
-- Chunks are SECTIONS, not fixed-size windows.
--
-- This is the decision that makes citations trustworthy. A 512-token sliding
-- window produces chunks that start mid-sentence and belong to no particular
-- part of the document, so the best you can cite is a document name. Cutting
-- on the numbered headings the SOP already has means every chunk arrives
-- with an anchor a quality manager can look up: "SOP-RESTART-FLAT section
-- 4.2". They can open the binder and check whether the agent read it right.
-- ---------------------------------------------------------------------
CREATE TABLE docs.chunks (
    chunk_id     serial PRIMARY KEY,
    doc_id       text NOT NULL REFERENCES docs.documents(doc_id) ON DELETE CASCADE,
    section_no   text NOT NULL,               -- '4.2'
    section_title text NOT NULL,
    ordinal      int NOT NULL,                -- position within the document
    content      text NOT NULL,
    -- Citation string is materialised rather than assembled at query time,
    -- so the exact token the model must echo back is a stored value that
    -- the Phase 3 validator can match against literally.
    citation     text NOT NULL,               -- 'SOP-RESTART-FLAT §4.2'
    token_estimate int NOT NULL,

    -- Lexical half of retrieval. Postgres full-text search: ts_rank_cd is
    -- not textbook BM25 (no document-length saturation term), but it is a
    -- real lexical scorer and it keeps the whole hybrid query inside one
    -- engine. Exact-term matching is what catches "sheeter", "metal
    -- detector", "82C" -- the vocabulary dense vectors are worst at.
    tsv          tsvector,

    -- Dense half. 384 dims = all-MiniLM-L6-v2, which runs on CPU in this
    -- repo. Nothing is sent anywhere to build this index -- the same
    -- argument as the Phase 4 hybrid LLM split, applied to embeddings.
    embedding    vector(384),

    UNIQUE (doc_id, section_no)
);

CREATE INDEX ON docs.chunks USING gin (tsv);
-- IVFFlat needs training data to be worth building and this corpus is tiny
-- (a few hundred chunks), so an exact scan is both faster and exact. The
-- index below is here as the thing you would create at corpus scale; at
-- this size Postgres will correctly ignore it.
CREATE INDEX ON docs.chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON docs.chunks (doc_id, ordinal);

-- Keep the tsvector in step with content automatically. Doing this in a
-- trigger rather than in the loader means a future loader written by
-- someone else cannot forget it.
CREATE FUNCTION docs.chunks_tsv_update() RETURNS trigger AS $$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('english', COALESCE(NEW.section_title, '')), 'A')
        || setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER chunks_tsv
    BEFORE INSERT OR UPDATE OF content, section_title ON docs.chunks
    FOR EACH ROW EXECUTE FUNCTION docs.chunks_tsv_update();
