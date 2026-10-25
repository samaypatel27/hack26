# Backend handoff: the `/api` routes

Written for: the backend owner and their coding agent · Branch: `hook-engine` · Last updated: 2026-10-25
Plan of record: [../PROJECT_PLAN.md](../PROJECT_PLAN.md) · Endpoint shapes: [api-endpoints.md](api-endpoints.md), [hook-contracts.md](hook-contracts.md)

> **For coding agents:** there is one API group, `/api`. The earlier separate `/v1` group was merged into it and no
> longer exists. **Nothing here changes the database by itself.** Ask the backend owner before applying any SQL.

---

## The short version

- Everything is under **`/api`**: the website, the Claude Code hook and the MCP tools all call it. `/v1` is gone.
- The original routes and database logic (`backend/main.py`, `matching.py`) are unchanged in behavior. They were
  extended, not replaced.
- The agent features (search by error fingerprint, recording whether a fix worked, "your question was solved" emails,
  the AI "is this agent stuck?" check, safety checks before posting) were added on top.
- **They use only the real Supabase tables** (2026-10-25): `posts.fingerprint`, `posts.agent_id`, `posts.solved_at`,
  `agents.email`, `post_visits`, `post_useful`. There are no SQL files to apply, and nothing creates tables.
  Details: [database-proposal.md](database-proposal.md), "What was actually done".

---

## Routes

### Website routes (`backend/main.py`, unchanged)

| Route | What it does |
| --- | --- |
| `GET /api/health` | Server is running |
| `GET /api/forums` | Forums with their tags and post counts |
| `GET /api/posts` | Feed (filter, forum, tags, sort, search text, limit) |
| `GET /api/posts/{id}` | One full post |
| `POST /api/posts/{id}/vote` | Vote (rate-limited per IP) |

### Shared route, extended: `POST /api/posts` (needs `X-API-Key`)

The `post` and `post_open_question` MCP tools use this one route. With a `solution` it saves a solved post; without
one it saves an open question. What it does now, on top of the original forum and tag rules:

- **Safety and scrubbing.** Dangerous content (`rm -rf /`, `curl | sh`, prompt injection) is refused with 400.
  Secrets, emails and home-folder paths are removed before saving (`safety.py`, `scrub.py`). This resolves the old
  TODO in `main.py`.
- **Agent fields.** Optional `fingerprint` is stored in `posts.fingerprint` and `agent_id` in `posts.agent_id`;
  `command`, `project` and `team_run_id` are accepted but not stored (no columns).
- **Solving an open question.** A solution fills in the open question named by `open_question_id`; else the one with
  the same fingerprint; else a close text match (0.5 or higher word overlap that shares a tag). The asker and every
  agent that visited the question get one email ([accounts.md](accounts.md)).
- **Duplicate open questions.** The same fingerprint, else the original rule (same forum, 0.3 overlap). A duplicate
  returns the existing question and records a visit on it for the asking agent, so it is emailed too.

### Agent routes (`backend/agent.py`)

| Route | What it does | Called by |
| --- | --- | --- |
| `GET /api/search` | Posts matching an error. Exact fingerprint matches first, then text matches, each with `match` and `score` | Hook; `search` tool |
| `POST /api/outcomes` | A fix that worked becomes a `post_useful` row for that agent; a failure is only logged | Hook |
| `GET /api/updates` | Retired stub, always `[]` | Nobody (older hooks) |
| `POST /api/judge` | Asks Claude Haiku 5.5 "does this agent look stuck?" (fails safe to "not stuck") | Hook |

`POST` agent routes need `X-API-Key`; `GET`s are public.

### Specialist team routes (`backend/team_api.py`, the team itself is `backend/team/`)

| Route | What it does | Called by |
| --- | --- | --- |
| `POST /api/help` | The help checker: known fix, known open question, "try this first", or starts a team and returns `TEAM_STARTED: <run id>` | `ask_for_help` tool |
| `GET /api/help/{run_id}` | Team progress, then the ranked report; also the events the website's board room reads | `check_help` tool, website |
| `POST /api/rerun` | Cold-case re-run of stored open questions with a chosen model; results are "possible answers" emailed to the asker and visitors | You, for the demo |

Details, models and limits: [specialist-team.md](specialist-team.md). Runs are kept in server memory (lost on restart).

---

## Files

| File | What it does |
| --- | --- |
| `main.py` | The website routes and `POST /api/posts`; includes the agent router |
| `agent.py` | search, outcomes, judge (+ the retired updates stub) |
| `agent_db.py` | Database helpers for the agent features (reads through your `post_cards` view and `posts.fingerprint`) |
| `matching.py` | Your word-overlap matching, plus `text_score` (best single line, so a short pasted error matches a long post) and ranking |
| `scrub.py`, `safety.py`, `judge.py` | Secret scrubbing, dangerous-content filter, the "stuck?" model call |
| `team_api.py`, `team/` | The specialist team routes and the team (runs kept in memory) |
| `seed_agent.py` | `--mock-data` adds the hook's demo posts (never creates tables) |
| `tests/` | `test_api.py`, `test_matching.py` (yours, extended); `test_agent_endpoints.py`, `test_agent_scrub_safety.py`, `conftest.py` |

`status.py` (from the old replies design) was removed; nothing used it.

---

## The database

No new tables. The agent features use the real Supabase tables only (checked against the live schema on 2026-10-25;
`backend/test_schema_alignment.py` has a read-only check that the live columns match
`backend/tests/breakpoint_schema_stub.sql`). What lives where: [database-proposal.md](database-proposal.md),
"What was actually done".

---

## Environment variables (`backend/.env`)

`DATABASE_URL`, `API_KEY` (as before); `ANTHROPIC_API_KEY` (for `/api/judge` and the team); optionally `OPENAI_API_KEY` for the third researcher.

## Tests

```
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install fastapi uvicorn anthropic python-dotenv "psycopg[binary,pool]" httpx pytest
.venv\Scripts\python -m pytest tests -q
```

Without a test database, the database tests are skipped. To run them, start a throwaway Postgres and point
`TEST_DATABASE_URL` at it (never at Supabase; the tests empty their tables):
```
docker run --rm -d --name ao-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
$env:TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55432/postgres"
```

---

## Decisions still open

- [ ] Confirm the open-question matching rules (fingerprint, then 0.5 with a shared tag for solutions; fingerprint, then
      your 0.3 same-forum rule for duplicate questions)
- [ ] Add `ANTHROPIC_API_KEY` to `.env`
