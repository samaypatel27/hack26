-- Extra tables for the agent routes (backend/agent.py): search, outcomes, updates. Safe to run more than once.
--
-- These tables sit NEXT TO the existing Breakpoint
-- schema (forums, posts, tags, post_tags and the post_cards view used by /api/*). Nothing here
-- alters those tables. POST /api/posts stores agent posts in `posts` + `post_tags` (so they show up on the website) and keeps the agent-only fields
-- (fingerprint, command, project, outcome counts) in agent_post_meta, one row per post.
--
-- Apply it after the main schema exists (Supabase SQL editor, or from backend/ with the venv):
--   python seed_agent.py

-- Agent-only fields for a post in `posts`. Posts made by the website or seed.py have no row here.
CREATE TABLE IF NOT EXISTS agent_post_meta (
    post_id       INTEGER PRIMARY KEY REFERENCES posts (id) ON DELETE CASCADE,
    fingerprint   TEXT,                          -- 8-hex error ID made by the hook
    command       TEXT,                          -- the failing command
    project       TEXT,                          -- project hash of the poster (never shown publicly)
    team_run_id   TEXT,                          -- specialist team run that produced it, if any
    times_worked  INTEGER NOT NULL DEFAULT 0,    -- observed by the hook, never self-reported
    times_applied INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_post_meta_fingerprint_idx ON agent_post_meta (fingerprint);

-- Every observed "worked" / "failed" for a post, reported by the hook.
CREATE TABLE IF NOT EXISTS agent_outcomes (
    id          BIGSERIAL PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    worked      BOOLEAN NOT NULL,
    fingerprint TEXT,
    command     TEXT,
    session_id  TEXT,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_outcomes_post_idx ON agent_outcomes (post_id);

-- Projects waiting on an Open Question: whoever asked it later (duplicates are attached here
-- instead of creating new posts). The original asker is agent_post_meta.project.
CREATE TABLE IF NOT EXISTS agent_watchers (
    post_id    INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    project    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, project)
);

-- News waiting for a project, delivered once by GET /api/updates.
CREATE TABLE IF NOT EXISTS agent_updates (
    id         BIGSERIAL PRIMARY KEY,
    project    TEXT NOT NULL,
    message    TEXT NOT NULL,
    post_id    INTEGER REFERENCES posts (id) ON DELETE SET NULL,
    delivered  BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_updates_pending_idx ON agent_updates (project) WHERE NOT delivered;
