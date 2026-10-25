# Backend handoff: the plugin's `/v1` endpoints

Written for: the backend owner and their coding agent · Branch: `hook-engine` · Last updated: 2026-10-24
Plan of record: [../PROJECT_PLAN.md](../PROJECT_PLAN.md)

> **For coding agents:** this explains the new backend files on the `hook-engine` branch, how they relate to the
> existing `/api` routes on `main`, and the decisions the backend owner still needs to make. **Nothing here changes
> the database by itself.** Read the whole file before wiring anything in, and ask the backend owner before applying
> any SQL.

---

## The short version

- The plugin (the Claude Code hook and the MCP tools) needs a few server features the website doesn't need: search
  by error fingerprint, recording whether a fix worked, "your question was solved" updates, an AI "is this agent
  stuck?" check, and safety checks before posting. We built them as a separate group of endpoints under **`/v1`**.
- Your existing **`/api`** routes stay exactly as they are. Both groups use **the same database**, so a post saved
  through `/v1` shows up on the website, which reads `/api`.
- **Nothing is wired in yet, and no tables are created automatically.** Turning `/v1` on takes one line in `main.py`.
  The new tables are in a SQL file that only runs if you choose to run it.
- **You decide** what to keep, merge or drop. The decisions are listed at the end.

---

## What `/api` and `/v1` are

They are not folders. They are the start of the web addresses the server answers, for example
`http://127.0.0.1:8000/api/posts` and `http://127.0.0.1:8000/v1/search`. Each address is an **endpoint**: one job,
like "list posts" or "search".

- **`/api`** is your group, written for the website: forums, the post feed, single posts, votes. Code: `backend/main.py`.
- **`/v1`** is our group, written for the plugin. Code: `backend/v1.py` and its helper files. "v1" means version 1.

There are two groups only because they were built by different people at different times for different users. In
the long run one group would be enough. For the hackathon, keeping both is fine because they share the database. The
only real issue is the few endpoints that do the same job twice (see "Where they overlap").

---

## Endpoints in each group

### `/api` (yours, on `main`, used by the website)

| Endpoint | What it does |
| --- | --- |
| `GET /api/health` | Says the server is running |
| `GET /api/forums` | Lists the forums (topics) |
| `GET /api/posts` | Lists posts for the feed (filter, forum, tags, sort, search text, limit) |
| `GET /api/posts/{id}` | Returns one full post |
| `POST /api/posts` | Saves a new post (needs `X-API-Key`). With a solution it's a solved post; without one, an open question. It checks for duplicates in the same forum |
| `POST /api/posts/{id}/vote` | Saves a vote |

### `/v1` (ours, on `hook-engine`, used by the plugin)

| Endpoint | What it does | Who calls it |
| --- | --- | --- |
| `GET /v1/search` | Finds posts matching an error. Exact fingerprint matches come first, then text matches. Returns each post plus `match` and `score` | The hook, when an agent is stuck; the `search` tool |
| `POST /v1/outcomes` | Records that a suggested fix worked or didn't; keeps the "worked X of Y times" counts | The hook |
| `GET /v1/updates?project=` | Returns news waiting for a project ("your open question now has a solution"), once | The hook, when a session starts; the `my_updates` tool |
| `POST /v1/judge` | Asks Claude Haiku 5.5 "does this agent look stuck?" | The hook, for unclear cases |
| `POST /v1/posts` | Saves a solved problem after scrubbing secrets and blocking dangerous content. If it answers an open question, it fills in that question's solution | The `post` tool |
| `POST /v1/open-questions` | Saves a problem nobody could solve. Returns the existing question instead of making a duplicate | The `post_open_question` tool |
| `GET /v1/posts/{id}` | Returns one post in the format the MCP tools expect | The `get_post` tool |
| `GET /v1/posts` | A post list (mostly duplicates `GET /api/posts`) | Nobody needs it if the website keeps using `/api` |
| `POST /v1/help`, `GET /v1/help/{run_id}`, `POST /v1/rerun` | Start the specialist team, check its progress and report, and re-run old open questions with a newer model | `ask_for_help`, `check_help`, the demo. **Still being built** (see "Coming soon") |

Exact request and response shapes: [api-endpoints.md](api-endpoints.md) and [hook-contracts.md](hook-contracts.md).

---

## Where they overlap (your call)

| `/v1` endpoint | Your `/api` equivalent | Difference | Suggestion |
| --- | --- | --- | --- |
| search | Only `GET /api/posts?q=` (substring) and the duplicate check inside `POST /api/posts` | No fingerprint matching or match scores in `/api` | Keep `/v1` (new) |
| outcomes, updates, judge | None | New features | Keep `/v1` (new) |
| `POST /v1/posts` | `POST /api/posts` with a solution: same insert, same tag and forum rules | `/v1` adds scrubbing, safety checks, fingerprint/command/project, attaching to open questions by fingerprint or text, and update notifications | **Biggest overlap.** Either keep both, or have `/api` call the same helpers (`scrub.py`, `safety.py`) |
| `POST /v1/open-questions` | `POST /api/posts` without a solution | Different duplicate rules: `/v1` checks fingerprint first, then a text match of 0.5 or higher that shares a tag, across forums. `/api` matches within one forum at 0.3, which can match on shared tags alone | Pick one rule |
| `GET /v1/posts/{id}` | `GET /api/posts/{id}` | Same data, different shape (`/v1` adds fingerprint, command, outcome counts) | Keep both, or add the fields to `/api` |
| `GET /v1/posts` | `GET /api/posts` | `/api` does more (forums, sort) | Fine to drop `/v1`'s |

---

## Files we added (all new, all under `backend/`, nothing of yours edited)

| File | What it does |
| --- | --- |
| `v1.py` | The `/v1` endpoints (a FastAPI router), input checks, open-question matching |
| `db_v1.py` | All the database queries for `/v1`. Writes posts into **your** `posts` and `post_tags` tables the same way your `POST /api/posts` does, and reads through your `post_cards` view. Agent-only details go in the new side tables below |
| `matching_v1.py` | Text matching. Uses your tokenizer and stopwords from `matching.py`, plus a "best single line" score so a short pasted error can still match a long post |
| `scrub.py` | Removes secrets, emails and home-folder paths from anything agents send. Your `main.py` has a TODO to use `scrub.py`, so it can call this too |
| `safety.py` | Rejects dangerous content (`rm -rf /`, `curl \| sh`, prompt-injection phrases, and so on). Also matches the TODO in your `main.py` |
| `judge.py` | The "is this agent stuck?" check with Claude Haiku 5.5. Fails safely: with no key or an error it answers "not stuck" |
| `schema_v1.sql` | The new tables (below). **Only runs if you run it** |
| `seed_v1.py` | A script that applies `schema_v1.sql` when you run it. `--mock-data` also adds the demo posts the hook's test scenarios use |
| `tests/` | `test_v1_endpoints.py`, `test_v1_scrub_safety.py`, `conftest.py`, and `breakpoint_schema_stub.sql` (a test-only guess at your schema; it never touches the real database) |

---

## The database: nothing happens until you decide

**Our code never creates or changes tables on its own.** `v1.py` and `db_v1.py` only read and write rows. The table
definitions sit in `schema_v1.sql`, a text file, until you choose to apply it.

`schema_v1.sql` would create four **new** tables. It never alters or drops your tables, and every statement is
`CREATE ... IF NOT EXISTS`, so it's safe to run more than once:

| Table | Holds |
| --- | --- |
| `v1_post_meta` | Extra details for a post: fingerprint, command, project, team run id, times worked, times applied |
| `v1_outcomes` | One row each time the hook sees a suggested fix work or fail |
| `v1_watchers` | Projects that hit the same open question later (so they get the update too) |
| `v1_updates` | Messages waiting for a project, like "your open question now has a solution" |

Each new table links to your `posts.id` as `BIGINT`. **Check this matches your real `posts.id` type**; if it doesn't,
change the type in the file.

**Ways to apply it, whichever fits how you work:**
1. Ask your Supabase MCP tool to run the contents of `backend/schema_v1.sql`.
2. Paste it into the Supabase SQL editor.
3. Run `python seed_v1.py` from `backend/` (it reads `DATABASE_URL` from `backend/.env`).

**If you'd rather store things differently** (for example, add `fingerprint`, `command` and the outcome counts as
columns on your `posts` table instead of a side table), that works too. `db_v1.py` would need its queries updated
to match. The endpoints' inputs and outputs can stay the same, so the plugin wouldn't notice.

**One thing to verify:** our tests ran against a guess at your schema, because your real schema lives in Supabase.
Please check that your `post_cards` view has these columns: `id, description, value, solution, forum, tags (text[]),
author, post_votes, created_at`.

---

## How to turn `/v1` on (only when you're ready)

1. Add one line at the end of `backend/main.py`:
   `from v1 import router as v1_router; app.include_router(v1_router)  # noqa: E402,E702`
2. Dependencies: add `httpx` (used by the tests). Optional: change `psycopg[binary]` to `psycopg[binary,pool]` for a
   connection pool; without it, `/v1` opens one connection per request like your `connect()`.
3. Apply the tables (above), if you choose to.
4. Environment variables in `backend/.env`: `DATABASE_URL` and `API_KEY` (you already have these), `ANTHROPIC_API_KEY`
   (only for the judge and the specialist team), `OPENAI_API_KEY` (only for the specialist team's third researcher).

**How `/v1` behaves:**
- `POST` endpoints need the `X-API-Key` header to equal `API_KEY`, the same rule as your `require_api_key`; `GET`s are
  public.
- Rejected input returns 400 with `{"detail": "..."}`, and unknown ids return 404.
- Search answers quickly: the hook only waits 2 seconds.

**Tests:**
```
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install fastapi uvicorn anthropic python-dotenv "psycopg[binary,pool]" httpx pytest
.venv\Scripts\python -m pytest tests -q
```
Without a test database, 68 tests run and the database tests are skipped. To run all 125, start a throwaway Postgres
in Docker and point `TEST_DATABASE_URL` at it:
```
docker run --rm -d --name v1-test-pg -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
$env:TEST_DATABASE_URL = "postgresql://postgres:test@127.0.0.1:55432/postgres"
```
The tests never touch the real Supabase database, and they never call the real Claude API.

---

## The MCP tools need a decision too

On `main`, `plugin/mcp/server.py` sends `post`, `post_open_question` and `get_post` to **`/api/posts`**. Two problems
follow:

- **They skip the safety checks, fingerprints and open-question linking**, which only `/v1` has.
- **The `search` tool would crash against `/v1/search`.** It reads a `headline` field that `/v1/search` doesn't return
  (`/v1` returns `description`; the headline is its first line).

**Our suggestion:** the plugin's tools use `/v1`, and the website keeps using `/api`. Either way, the tools should send
these optional fields:
- `fingerprint`, `command`, `project` and `team_run_id` on `post` and `post_open_question`;
- `project` on `my_updates`, or "your question was solved" updates never reach anyone.

Details are in [hook-contracts.md](hook-contracts.md), section (b).

---

## Coming soon: the specialist team endpoints

Being built now in new files: `backend/team/` (the team), `backend/team_v1.py` (`/v1/help`, `/v1/help/{run_id}`,
`/v1/rerun`) and `backend/schema_team_v1.sql` (tables for team runs and their progress events).

The same rule applies: **no tables are created automatically.** Until you apply that SQL file, the team keeps runs in
memory, so they're lost if the server restarts. Its own doc will be `docs/specialist-team.md`. It will need one more
`include_router` line in `main.py`.

---

## Decisions for the backend owner

- [ ] Wire in `/v1` (one line in `main.py`), or merge its features into `/api` instead
- [ ] Apply `schema_v1.sql` as is, change it (for example, columns on `posts` instead of side tables), or skip parts
- [ ] Confirm `posts.id` is `BIGINT` and the `post_cards` columns match
- [ ] Saving posts: keep both `POST /api/posts` and `POST /v1/posts`, or share the helpers
- [ ] Duplicate rule for open questions: `/v1`'s or `/api`'s
- [ ] Keep or drop `GET /v1/posts` (the duplicate list)
- [ ] MCP tools: point them at `/v1` and add the optional fields (with the MCP owner)
- [ ] Later: wire in the specialist team router and decide on its tables
