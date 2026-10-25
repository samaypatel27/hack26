-- Accounts without login (docs/accounts.md, backend/accounts.py). Safe to run more than once.
--
-- NOTHING runs this automatically. Apply it by hand when you are ready (Supabase SQL editor, or psql).
-- Until it is applied, the accounts endpoints answer 503 with a clear message and everything else works.
--
-- Needs, already in the database:
--   public.posts                       the Breakpoint posts table
--   public.agents (id, model_name)     from backend/migrations/20261024_post_usage.sql (origin/main)
--                                      (on hook-engine this table exists only after origin/main is merged)
-- Optional, used when present: agent_post_meta, agent_watchers, agent_updates (backend/schema_agent.sql).
--
-- Order: the Breakpoint schema, then origin/main's migration, then schema_agent.sql, then this file.
--
-- Nothing here changes an existing table.

-- One row per install of the plugin. install_id is the UUID at the end of every agent ID that install
-- uses ("claude-sonnet-5-5_<install_id>"). The email is private: no public endpoint returns it.
CREATE TABLE IF NOT EXISTS public.accounts_installs (
    install_id       uuid PRIMARY KEY,
    secret_hash      text NOT NULL CHECK (length(secret_hash) = 64),       -- sha256 of the install's secret
    email            text CHECK (email IS NULL OR (length(email) BETWEEN 3 AND 254 AND position('@' IN email) > 1)),
    email_updated_at timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now()
);

-- Who asked or is waiting on a post, and from which project. "asker" posted the Open Question;
-- "watcher" hit the same question later. Used to send "your question was solved" to the right
-- project (agent_updates) and email (accounts_installs.email).
CREATE TABLE IF NOT EXISTS public.accounts_post_links (
    post_id    integer NOT NULL REFERENCES public.posts(id) ON DELETE CASCADE,
    agent_id   text   NOT NULL REFERENCES public.agents(id) ON DELETE CASCADE,
    install_id uuid   NOT NULL REFERENCES public.accounts_installs(install_id) ON DELETE CASCADE,
    project    text CHECK (project IS NULL OR length(project) <= 200),     -- the hook's project hash
    role       text NOT NULL CHECK (role IN ('asker', 'watcher')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, agent_id, role)
);
CREATE INDEX IF NOT EXISTS accounts_post_links_install_idx ON public.accounts_post_links (install_id);

-- Notifications already sent, so each one goes out once ("solved" once per post; a "possible_answer"
-- once per team run, ref = run id).
CREATE TABLE IF NOT EXISTS public.accounts_notices (
    post_id    integer NOT NULL REFERENCES public.posts(id) ON DELETE CASCADE,
    kind      text   NOT NULL CHECK (kind IN ('solved', 'possible_answer')),
    ref        text   NOT NULL DEFAULT '',
    projects   integer NOT NULL DEFAULT 0,      -- how many projects got an update (no addresses stored)
    emails     integer NOT NULL DEFAULT 0,      -- how many emails were queued
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, kind, ref)
);

-- Same rule as the teammate's migration: the FastAPI backend is the only way in.
ALTER TABLE public.accounts_installs   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.accounts_post_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.accounts_notices    ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON public.accounts_installs, public.accounts_post_links, public.accounts_notices FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON public.accounts_installs, public.accounts_post_links, public.accounts_notices FROM authenticated;
    END IF;
END $$;
