-- Tables for the specialist team (backend/team/, backend/team_v1.py). Safe to run more than once.
--
-- PROPOSAL FOR THE BACKEND OWNER TO REVIEW AND APPLY BY HAND (Supabase SQL editor or psql).
-- Nothing in the code creates or changes these tables. Until they exist, the team keeps its runs
-- in memory (lost on restart) and logs "team tables not found".
--
-- Creates exactly two tables and two indexes; does not touch any existing table:
--   v1_team_runs    one row per team run (status, case file, final report, cost)
--   v1_team_events  the board room: one row per step (member started, search query, source found,
--                   member finished, chair started/done). The website can subscribe to inserts here.

CREATE TABLE IF NOT EXISTS v1_team_runs (
    id               TEXT PRIMARY KEY,                    -- "tr_" + 12 hex, returned as TEAM_STARTED: <id>
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'report', 'no_answer', 'failed')),
    source           TEXT NOT NULL DEFAULT 'help',        -- 'help' (POST /v1/help) or 'rerun' (POST /v1/rerun)
    fingerprint      TEXT,                                -- hook's error id, to attach a second asker to a running run
    project          TEXT,                                -- asker's project hash (never shown publicly)
    open_question_id BIGINT,                              -- the Open Question post a re-run is about
    models           JSONB,                               -- model per seat for this run
    case_file        JSONB,                               -- the scrubbed case file the team saw
    report           JSONB,                               -- final report (docs/api-endpoints.md #10)
    cost_usd         NUMERIC(10, 4) NOT NULL DEFAULT 0,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    deadline_at      TIMESTAMPTZ NOT NULL                 -- past this (+15s) a 'running' row is marked failed
);
CREATE INDEX IF NOT EXISTS v1_team_runs_active_idx ON v1_team_runs (fingerprint) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS v1_team_events (
    id         BIGSERIAL PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES v1_team_runs (id) ON DELETE CASCADE,
    member     TEXT NOT NULL,                             -- docs | issues | community | chair | runner
    type       TEXT NOT NULL,                             -- started | searching | fetching | source | fallback |
                                                          -- tool_error | finished | failed | timeout | skipped |
                                                          -- chair_started | chair_done | done
    text       TEXT NOT NULL DEFAULT '',
    data       JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v1_team_events_run_idx ON v1_team_events (run_id, id);
