-- TEST ONLY. NOT the real Breakpoint schema, which lives in Supabase and is owned by the backend
-- teammate. Reconstructed from the columns origin/main backend/main.py and seed.py read and
-- write, so the agent route tests can run against a throwaway Postgres. If the real schema differs,
-- trust the real one.

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
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS post_tags (
    post_id INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags (id),
    PRIMARY KEY (post_id, tag_id)
);

CREATE OR REPLACE VIEW post_cards AS
SELECT p.id, p.description, p.value, p.solution, p.author, p.status, p.post_votes, p.solution_votes,
       p.created_at, f.slug AS forum, f.name AS forum_name,
       COALESCE(array_agg(t.slug ORDER BY t.slug) FILTER (WHERE t.slug IS NOT NULL), ARRAY[]::text[]) AS tags
FROM posts p
JOIN forums f ON f.id = p.forum_id
LEFT JOIN post_tags pt ON pt.post_id = p.id
LEFT JOIN tags t ON t.id = pt.tag_id
GROUP BY p.id, f.slug, f.name;
