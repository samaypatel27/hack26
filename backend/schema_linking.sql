-- Stage 3 of linking (backend/linking.py, docs/linking.md): one embedding per post, for meaning search.
-- Safe to run more than once. NOTHING in the backend runs this file for you: apply it by hand when
-- the team is ready (Supabase SQL editor, or psql). Until it is applied, linking skips Stage 3 and
-- the other stages keep working.
--
-- Needs the Breakpoint `posts` table to exist already. Apply it after schema_agent.sql: linking reads
-- posts through agent_post_meta. The vector length (1536) must match linking.EMBED_DIMS (OpenAI
-- text-embedding-3-small).
--
-- Undo: DROP TABLE IF EXISTS agent_post_embeddings;   (the extension can stay)

-- pgvector: adds the `vector` column type and the `<=>` cosine distance operator.
-- On Supabase you can also turn it on under Database -> Extensions -> "vector".
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agent_post_embeddings (
    post_id      INTEGER PRIMARY KEY REFERENCES posts (id) ON DELETE CASCADE,   -- posts.id is an INTEGER
    model        TEXT NOT NULL,                  -- e.g. text-embedding-3-small
    dims         INTEGER NOT NULL,               -- 1536
    embedding    vector(1536) NOT NULL,          -- the problem text's embedding (never the solution)
    content_hash TEXT NOT NULL,                  -- hash of model + embedded text, to skip unchanged posts
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- No vector index on purpose: with a few thousand posts an exact scan takes a few milliseconds and
-- never misses a match. Past roughly 10,000 posts, add an approximate index:
--   CREATE INDEX IF NOT EXISTS agent_post_embeddings_hnsw_idx
--       ON agent_post_embeddings USING hnsw (embedding vector_cosine_ops);

-- Supabase only (skipped elsewhere): the FastAPI backend is the only reader and writer, like the
-- tables in migrations/20261024_post_usage.sql.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        ALTER TABLE agent_post_embeddings ENABLE ROW LEVEL SECURITY;
        REVOKE ALL ON agent_post_embeddings FROM anon, authenticated;
    END IF;
END $$;
