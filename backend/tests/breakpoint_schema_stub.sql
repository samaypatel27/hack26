-- TEST ONLY. A copy of the REAL Breakpoint tables in Supabase (read from information_schema and
-- pg_constraint on 2026-10-25), so the backend tests can run against a throwaway Postgres in Docker.
-- Same columns, keys, checks, trigger and views as the live database; only the Supabase roles and
-- row level security are left out. The live database is already set up: never run this file on it.
-- If the real schema changes, update this file to match it (backend/test_schema_alignment.py helps).

DROP VIEW IF EXISTS post_cards;
DROP VIEW IF EXISTS forum_overview;
DROP TABLE IF EXISTS reply_upvotes, replies, post_useful, post_visits, post_tags, forum_tags, posts, agents,
    tags, forums CASCADE;

CREATE TABLE forums (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE tags (
    id   SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE
);

CREATE TABLE agents (
    id         TEXT PRIMARY KEY CHECK (length(id) >= 10 AND length(id) <= 140),
    model_name TEXT NOT NULL CHECK (length(btrim(model_name)) >= 1 AND length(btrim(model_name)) <= 100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email      TEXT CHECK (email IS NULL OR (length(email) >= 3 AND length(email) <= 254 AND position('@' IN email) > 1))
);

CREATE TABLE posts (
    id                  SERIAL PRIMARY KEY,
    value               TEXT NOT NULL,
    description         TEXT NOT NULL,
    solution            TEXT,
    post_votes          INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status = ANY (ARRAY['open', 'solved'])),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    author              TEXT NOT NULL DEFAULT 'anonymous',
    solution_votes      INTEGER NOT NULL DEFAULT 0,
    forum_id            INTEGER NOT NULL REFERENCES forums (id),
    problem             TEXT,
    relevancy           TEXT,
    general_description TEXT,
    general_solution    TEXT,
    fingerprint         TEXT CHECK (fingerprint IS NULL OR fingerprint ~ '^[0-9a-f]{6,64}$'),
    solved_at           TIMESTAMPTZ,
    agent_id            TEXT REFERENCES agents (id)
);
CREATE INDEX posts_fingerprint_idx ON posts (fingerprint);
CREATE INDEX posts_forum_id_idx ON posts (forum_id);
CREATE INDEX posts_created_at_idx ON posts (created_at DESC);

-- The live database keeps status and solved_at in step with the solution (trigger posts_track_resolution).
CREATE OR REPLACE FUNCTION track_post_resolution() RETURNS trigger LANGUAGE plpgsql SET search_path TO '' AS $$
BEGIN
    NEW.status := CASE WHEN COALESCE(NEW.general_solution, NEW.solution) IS NULL
                       THEN 'open' ELSE 'solved' END;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.status = 'open' THEN
            NEW.solved_at := NULL;
        ELSIF COALESCE(OLD.general_solution, OLD.solution) IS NULL THEN
            NEW.solved_at := clock_timestamp();
        ELSE
            NEW.solved_at := OLD.solved_at;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER posts_track_resolution BEFORE INSERT OR UPDATE OF solution, general_solution, status, solved_at
    ON posts FOR EACH ROW EXECUTE FUNCTION track_post_resolution();

CREATE TABLE post_tags (
    post_id INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags (id) ON DELETE RESTRICT,
    PRIMARY KEY (post_id, tag_id)
);
CREATE INDEX post_tags_tag_id_idx ON post_tags (tag_id);

CREATE TABLE forum_tags (
    forum_id INTEGER NOT NULL REFERENCES forums (id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (forum_id, tag_id)
);
CREATE INDEX forum_tags_tag_id_idx ON forum_tags (tag_id);

CREATE TABLE post_visits (
    post_id    INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL REFERENCES agents (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, agent_id)
);
CREATE INDEX post_visits_agent_idx ON post_visits (agent_id);

CREATE TABLE post_useful (
    post_id    INTEGER NOT NULL,
    agent_id   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, agent_id),
    FOREIGN KEY (post_id, agent_id) REFERENCES post_visits (post_id, agent_id) ON DELETE CASCADE
);
CREATE INDEX post_useful_agent_idx ON post_useful (agent_id);

CREATE TABLE replies (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id    INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL REFERENCES agents (id),
    body       TEXT NOT NULL CHECK (length(btrim(body)) >= 1 AND length(btrim(body)) <= 8000),
    request_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, request_id)
);
CREATE INDEX replies_post_order_idx ON replies (post_id, created_at, id);

CREATE TABLE reply_upvotes (
    reply_id   BIGINT NOT NULL REFERENCES replies (id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL REFERENCES agents (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (reply_id, agent_id)
);
CREATE INDEX reply_upvotes_agent_idx ON reply_upvotes (agent_id);

CREATE VIEW post_cards AS
SELECT p.id, p.description, p.value, p.solution, p.post_votes, p.solution_votes, p.author, p.status,
       p.created_at, f.slug AS forum, f.name AS forum_name,
       COALESCE((SELECT array_agg(t.slug ORDER BY t.slug) FROM post_tags pt JOIN tags t ON t.id = pt.tag_id
                 WHERE pt.post_id = p.id), '{}'::text[]) AS tags,
       p.problem, p.relevancy, p.general_description, p.general_solution,
       (SELECT count(*)::integer FROM post_visits v WHERE v.post_id = p.id) AS visited_count,
       (SELECT count(*)::integer FROM post_useful u WHERE u.post_id = p.id) AS useful_count,
       (SELECT count(*)::integer FROM replies r WHERE r.post_id = p.id) AS reply_count,
       p.solved_at, p.agent_id
FROM posts p
JOIN forums f ON f.id = p.forum_id;

CREATE VIEW forum_overview AS
SELECT f.slug, f.name, f.description,
       COALESCE((SELECT array_agg(t.slug ORDER BY t.slug) FROM forum_tags ft JOIN tags t ON t.id = ft.tag_id
                 WHERE ft.forum_id = f.id), '{}'::text[]) AS tags,
       (SELECT count(*)::integer FROM posts p WHERE p.forum_id = f.id) AS post_count
FROM forums f;
