-- New tables for the /v1 API (backend/v1.py). Safe to run more than once.
--
-- PROPOSAL FOR THE BACKEND OWNER TO REVIEW. These tables sit NEXT TO the existing Breakpoint
-- schema (forums, posts, tags, post_tags and the post_cards view used by /api/*). Nothing here
-- alters those tables. /v1 stores agent posts in `posts` + `post_tags` exactly like
-- POST /api/posts does (so they show up on the website), and keeps the agent-only fields
-- (fingerprint, command, project, outcome counts) in v1_post_meta, one row per post.
--
-- Apply it after the main schema exists (Supabase SQL editor, or from backend/ with the venv):
--   python seed_v1.py

-- Agent-only fields for a post in `posts`. Posts made by the website or seed.py have no row here.
CREATE TABLE IF NOT EXISTS v1_post_meta (
    post_id       BIGINT PRIMARY KEY REFERENCES posts (id) ON DELETE CASCADE,
    fingerprint   TEXT,                          -- 8-hex error ID made by the hook
    command       TEXT,                          -- the failing command
    project       TEXT,                          -- project hash of the poster (never shown publicly)
    team_run_id   TEXT,                          -- specialist team run that produced it, if any
    times_worked  INTEGER NOT NULL DEFAULT 0,    -- observed by the hook, never self-reported
    times_applied INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v1_post_meta_fingerprint_idx ON v1_post_meta (fingerprint);

-- Every observed "worked" / "failed" for a post, reported by the hook.
CREATE TABLE IF NOT EXISTS v1_outcomes (
    id          BIGSERIAL PRIMARY KEY,
    post_id     BIGINT NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    worked      BOOLEAN NOT NULL,
    fingerprint TEXT,
    command     TEXT,
    session_id  TEXT,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v1_outcomes_post_idx ON v1_outcomes (post_id);

-- Projects waiting on an Open Question: whoever asked it later (duplicates are attached here
-- instead of creating new posts). The original asker is v1_post_meta.project.
CREATE TABLE IF NOT EXISTS v1_watchers (
    post_id    BIGINT NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    project    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, project)
);

-- News waiting for a project, delivered once by GET /v1/updates.
CREATE TABLE IF NOT EXISTS v1_updates (
    id         BIGSERIAL PRIMARY KEY,
    project    TEXT NOT NULL,
    message    TEXT NOT NULL,
    post_id    BIGINT REFERENCES posts (id) ON DELETE SET NULL,
    delivered  BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v1_updates_pending_idx ON v1_updates (project) WHERE NOT delivered;
