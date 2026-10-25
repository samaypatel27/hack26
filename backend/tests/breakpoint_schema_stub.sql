-- TEST ONLY. NOT the real Breakpoint schema, which lives in Supabase and is owned by the backend
-- teammate. Reconstructed from the columns backend/main.py and seed.py read and write, plus
-- backend/migrations/20261024_post_usage.sql, so the agent route tests can run against a throwaway
-- Postgres. If the real schema differs, trust the real one.

CREATE TABLE IF NOT EXISTS forums (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    id   SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS posts (
    id             SERIAL PRIMARY KEY,
    description    TEXT NOT NULL,
    value          TEXT NOT NULL,
    solution       TEXT,
    author         TEXT NOT NULL DEFAULT 'anonymous',
    forum_id       INTEGER NOT NULL REFERENCES forums (id),
    status         TEXT NOT NULL DEFAULT 'open',
    post_votes     INTEGER NOT NULL DEFAULT 0,
    solution_votes INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- backend/migrations/20261024_post_usage.sql (the post structure)
    problem             TEXT,
    relevancy           TEXT,
    general_description TEXT,
    general_solution    TEXT
);
ALTER TABLE posts ADD COLUMN IF NOT EXISTS problem TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS relevancy TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS general_description TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS general_solution TEXT;

CREATE TABLE IF NOT EXISTS post_tags (
    post_id INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags (id),
    PRIMARY KEY (post_id, tag_id)
);

CREATE TABLE IF NOT EXISTS forum_tags (
    forum_id INTEGER NOT NULL REFERENCES forums (id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (forum_id, tag_id)
);

-- Agents and their activity (backend/migrations/20261024_post_usage.sql, without the Supabase roles).
CREATE TABLE IF NOT EXISTS agents (
    id         TEXT PRIMARY KEY CHECK (length(id) BETWEEN 38 AND 140),
    model_name TEXT NOT NULL CHECK (length(btrim(model_name)) BETWEEN 1 AND 100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS post_visits (
    post_id    INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL REFERENCES agents (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, agent_id)
);
CREATE TABLE IF NOT EXISTS post_useful (
    post_id    INTEGER NOT NULL,
    agent_id   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, agent_id),
    FOREIGN KEY (post_id, agent_id) REFERENCES post_visits (post_id, agent_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS replies (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id    INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL REFERENCES agents (id),
    body       TEXT NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 8000),
    request_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, request_id)
);
CREATE TABLE IF NOT EXISTS reply_upvotes (
    reply_id   BIGINT NOT NULL REFERENCES replies (id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL REFERENCES agents (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (reply_id, agent_id)
);

DROP VIEW IF EXISTS post_cards;
CREATE VIEW post_cards AS
SELECT p.id, p.description, p.value, p.solution, p.author, p.status, p.post_votes, p.solution_votes,
       p.created_at, f.slug AS forum, f.name AS forum_name,
       COALESCE((SELECT array_agg(t.slug ORDER BY t.slug) FROM post_tags pt JOIN tags t ON t.id = pt.tag_id
                 WHERE pt.post_id = p.id), ARRAY[]::text[]) AS tags,
       p.problem, p.relevancy, p.general_description, p.general_solution,
       (SELECT count(*)::integer FROM post_visits v WHERE v.post_id = p.id) AS visited_count,
       (SELECT count(*)::integer FROM post_useful u WHERE u.post_id = p.id) AS useful_count,
       (SELECT count(*)::integer FROM replies r WHERE r.post_id = p.id) AS reply_count
FROM posts p
JOIN forums f ON f.id = p.forum_id;
