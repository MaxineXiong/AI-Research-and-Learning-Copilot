-- =========================================================================
-- AI Research & Learning Copilot — Lakebase Postgres Schema
-- =========================================================================
-- Run this script in the Databricks Lakebase SQL Editor to create all
-- tables needed by the application.  It is safe to re-run (IF NOT EXISTS).
-- =========================================================================

-- Enable pgvector for embedding storage
CREATE EXTENSION IF NOT EXISTS vector;

-- =========================================================================
-- 1. USERS
-- =========================================================================
CREATE TABLE IF NOT EXISTS users (
    user_id        BIGSERIAL    PRIMARY KEY,
    email          VARCHAR(255) NOT NULL UNIQUE,
    display_name   VARCHAR(255) NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- =========================================================================
-- 2. LEARNING GOALS
-- =========================================================================
CREATE TABLE IF NOT EXISTS learning_goals (
    goal_id        BIGSERIAL    PRIMARY KEY,
    user_id        BIGINT       NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title          VARCHAR(500) NOT NULL,
    description    TEXT,
    status         VARCHAR(20)  NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'completed', 'archived')),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_learning_goals_user
    ON learning_goals (user_id);

-- =========================================================================
-- 3. PAPERS  (OpenAlex works use IDs like "W1234567890")
-- =========================================================================
CREATE TABLE IF NOT EXISTS papers (
    paper_id           VARCHAR(64)  PRIMARY KEY,   -- OpenAlex work ID
    title              TEXT         NOT NULL,
    abstract           TEXT,
    publication_date   DATE,
    doi                VARCHAR(255),
    cited_by_count     INT          DEFAULT 0,
    source_name        VARCHAR(500),               -- journal / conference
    pdf_url            TEXT,
    openalex_url       TEXT,
    concepts           JSONB,                      -- topic/concept tags from OpenAlex
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- =========================================================================
-- 4. AUTHORS
-- =========================================================================
CREATE TABLE IF NOT EXISTS authors (
    author_id      VARCHAR(64)  PRIMARY KEY,       -- OpenAlex author ID
    display_name   VARCHAR(500) NOT NULL,
    institution    VARCHAR(500),
    orcid          VARCHAR(50),
    openalex_url   TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- =========================================================================
-- 5. PAPER ↔ AUTHOR  (many-to-many)
-- =========================================================================
CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id       VARCHAR(64)  NOT NULL REFERENCES papers(paper_id)   ON DELETE CASCADE,
    author_id      VARCHAR(64)  NOT NULL REFERENCES authors(author_id) ON DELETE CASCADE,
    position       VARCHAR(20),                    -- first / middle / last
    PRIMARY KEY (paper_id, author_id)
);

-- =========================================================================
-- 6. COLLECTIONS
-- =========================================================================
CREATE TABLE IF NOT EXISTS collections (
    collection_id  BIGSERIAL    PRIMARY KEY,
    user_id        BIGINT       NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name           VARCHAR(255) NOT NULL,
    description    TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_collections_user
    ON collections (user_id);

-- =========================================================================
-- 7. COLLECTION ↔ PAPER  (many-to-many)
-- =========================================================================
CREATE TABLE IF NOT EXISTS collection_papers (
    collection_id  BIGINT       NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
    paper_id       VARCHAR(64)  NOT NULL REFERENCES papers(paper_id)           ON DELETE CASCADE,
    added_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    notes          TEXT,
    PRIMARY KEY (collection_id, paper_id)
);

-- =========================================================================
-- 8. READING PROGRESS
-- =========================================================================
CREATE TABLE IF NOT EXISTS reading_progress (
    progress_id    BIGSERIAL    PRIMARY KEY,
    user_id        BIGINT       NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
    paper_id       VARCHAR(64)  NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    status         VARCHAR(20)  NOT NULL DEFAULT 'not_started'
                       CHECK (status IN ('not_started', 'reading', 'completed')),
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    UNIQUE (user_id, paper_id)
);

-- =========================================================================
-- 9. NOTES
-- =========================================================================
CREATE TABLE IF NOT EXISTS notes (
    note_id        BIGSERIAL    PRIMARY KEY,
    user_id        BIGINT       NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
    paper_id       VARCHAR(64)  NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    content        TEXT         NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notes_user_paper
    ON notes (user_id, paper_id);

-- =========================================================================
-- 10. EMBEDDINGS  (pgvector — abstracts, notes, goals)
-- =========================================================================
CREATE TABLE IF NOT EXISTS embeddings (
    id             VARCHAR(255) PRIMARY KEY,
    source_type    VARCHAR(20)  NOT NULL
                       CHECK (source_type IN ('abstract', 'note', 'goal')),
    source_id      VARCHAR(64)  NOT NULL,          -- paper_id, note_id, or goal_id
    chunk_index    INT          NOT NULL DEFAULT 0,
    chunk_text     TEXT         NOT NULL,
    embedding      VECTOR(384)  NOT NULL,           -- all-MiniLM-L6-v2 output dim
    model_name     VARCHAR(255) NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_embeddings_source
    ON embeddings (source_type, source_id);

CREATE INDEX IF NOT EXISTS idx_embeddings_vector_hnsw
    ON embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- =========================================================================
-- SEED DATA  — one default user for demo purposes
-- =========================================================================
INSERT INTO users (email, display_name)
VALUES ('demo@example.com', 'Demo User')
ON CONFLICT (email) DO NOTHING;

-- =========================================================================
-- VERIFICATION
-- =========================================================================
SELECT table_name
FROM   information_schema.tables
WHERE  table_schema = 'public'
  AND  table_type   = 'BASE TABLE'
ORDER  BY table_name;
