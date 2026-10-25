# Full scan report

Date: 2026-10-25. Branch: hook-engine. Five reviewers checked the project: end-to-end workflows, backend and database, plugin, website, and repo hygiene with docs and security. The chair merged their reports, removed duplicates, moved in-progress work to its own list, and spot-checked the main findings in the code. Nothing was changed except this file.

## Verdict

The project is close to ready. The main flows connect from end to end: stuck agent, forum search, team, local tester, post, linking and the "solved" email. The tests pass, apart from failures caused by the rename that is still in progress. Two things must be fixed before any demo. First, the website's reply list crashes any post that has a reply. Second, no API key is set, so every write to the backend fails without any message. After those, fix the public /docs page, which judges will read, and check that the old Supabase key from git history no longer works.

## Blockers

### B1. A post page crashes when the post has a reply
- **What is wrong:** The backend stopped sending `agent_id` on replies, for privacy. It now sends `author` and `name`. The website still reads `reply.agent_id` and passes it to `shortAgentId`, which calls `.replace` on undefined and throws. The site has no error page, so the whole post page breaks. Posts with no replies look fine, which is why nobody has noticed yet. The reply nudge being built now will make replies common. Two reviewers reported this. The chair confirmed it in the code.
- **Where:** `frontend/components/AgentReplies.tsx:49`, `frontend/lib/types.ts` (Reply type, about lines 35-47), `frontend/lib/format.ts:19-21`. The backend side is `backend/activity.py:111-116` (`public_reply`).
- **How to fix:** In `types.ts`, change Reply to have `author: string; name: string; model_name: string` and remove `agent_id`. In `AgentReplies.tsx`, show `{reply.author}` with `title={reply.name}`. Stop calling `shortAgentId`, and stop showing the full ID in the tooltip. Then open a post that has a reply to test it. This takes about 10 minutes.
- **Who:** frontend.

### B2. No API key is set, so every write route answers 401
- **What is wrong:** `backend/.env` has no `API_KEY` line, and `~/.breakpoint/config.json` has no `api_key`. The chair checked the key names only, not the values. `require_api_key` refuses every request when the key is empty. So posting, outcomes, judge, register, help, rerun, visits, useful marks, replies and upvotes all fail. The hook ignores errors on purpose, so the demo would just look like nothing happens. Two reviewers reported this.
- **Where:** `backend/.env`, `C:/Users/harsh/.breakpoint/config.json`, `backend/main.py:65-68`, `backend/agent.py:40-44`, `backend/accounts.py`.
- **How to fix:** Add `API_KEY=<long random text>` to `backend/.env`. Put the same value as `"api_key"` in `~/.breakpoint/config.json` (or set `BREAKPOINT_API_KEY`) on every demo machine. Send one request with the key to check it works. Add this step to the dry-run checklist in `docs/testing-guide.md`.
- **Who:** backend (setup).

## High

### H1. An old Supabase service_role key is still in git history
- **What is wrong:** A real legacy service_role key was committed in `backend/.env.example` in commit 457e8c7. Commit e072829 removed it, and cb31139 switched the code to the new keys. In Supabase, the old legacy keys keep working until someone turns them off or rotates the JWT secret. A service_role key skips all row-level security. The repo has a GitHub remote. Nobody could check from here whether the old key still works.
- **Where:** git history of `backend/.env.example`.
- **How to fix:** In the Supabase dashboard (Settings > API Keys), turn off the legacy JWT keys or rotate the JWT secret. Then confirm the old key fails. Rewriting git history is optional once the key is dead. Also remove `SUPABASE_SECRET_KEY` from the local `backend/.env`, because no code reads it.
- **Who:** backend (owner of the Supabase project).

### H2. The public /docs page describes an old version of the project
- **What is wrong:** Judges will read this page, and much of it is now false. It says the hook calls `run_specialists` and that `run_specialists` raises NotImplementedError. It says the tools call `/v1/*` routes that do not exist, and it lists `my_updates`. It calls the hook "pseudocode" in `hook/loop_detector.py`, a file that no longer exists. It says the asker hears back "at their next session start". It also says there is no scrubbing, no safety check, no fix tracking and no accounts. It describes votes, which the site no longer shows. It never mentions the board room, the quality check, the local tester, rewind, replies, useful marks or email notices. Four reviewers reported this. The chair confirmed it, for example at page.tsx lines 69, 90, 303-304, 333 and 407-408.
- **Where:** `frontend/app/docs/page.tsx`, `frontend/components/docs/Diagrams.tsx`, `frontend/components/docs/SystemDiagram.tsx`, `frontend/components/docs/LiveBoard.tsx`, `frontend/components/docs/TwoThings.tsx`.
- **How to fix:** Rewrite the page from the current `docs/*.md` files. List the 9 real tools: search, get_post, ask_for_help, check_help, post, post_open_question, useful, reply, upvote_reply. Use `/api` routes. Say notices are by email only. Describe the server team plus the local tester, and usefulness (useful marks out of visits) instead of votes. Link to /board-room. If time is short, at least delete the "Limits" list and the run_specialists, my_updates and /v1 lines. Start after the rename agent is done with `Diagrams.tsx`, and include LEAVE_REPLY once the reply work lands. This takes about 1 to 2 hours.
- **Who:** frontend and docs.

### H3. The hook does not see commands run with the PowerShell tool
- **What is wrong:** The hook matcher only lists Bash, the edit tools, Read, Grep, Glob and MCP tools. `notebook.action_from_tool_use` only turns `Bash` into a run record. On Windows, Claude Code also has a PowerShell tool, and the tester code already expects agents to use it. When an agent runs tests with PowerShell, the hook records nothing: no struggle signals, no KNOWN_FIX check, no outcome, no NOT_DONE and no checkpoints. The demo runs on Windows. Nobody measured how often agents pick PowerShell, so the real impact is not certain.
- **Where:** `plugin/hooks/hooks.json:16,22`, `plugin/hook/notebook.py` (`action_from_tool_use`, about line 315).
- **How to fix:** Add `PowerShell` to both matchers. Treat `tool_name == "PowerShell"` like `Bash` in `action_from_tool_use`. Add one test with a PowerShell failure payload. Do this after the rename and reply-nudge agents are done with these files.
- **Who:** plugin.

## Medium

### M1. Replies are saved and shown publicly without scrubbing or the safety check
- **What is wrong:** `POST /api/posts/{id}/replies` saves the body exactly as sent. It never calls `scrub()` or `safety.check()`, unlike `POST /api/posts`. A key, an email address or a `C:\Users\<name>` path in a reply would go public, and the reply work will soon show replies in search results too. The chair confirmed this in the code.
- **Where:** `backend/activity.py:171-184`. Also the MCP `reply` tool in `plugin/mcp/server.py`.
- **How to fix:** Run `safety.check(data.body)` (answer 400 if it refuses) and `data.body = scrub(data.body)` before the INSERT. Compare the scrubbed body in the 409 check. Add a test.
- **Who:** backend.

### M2. Backup matchers in main.py attach fixes at lower confidence than linking allows, and call it "solved"
- **What is wrong:** When `linking.link_new_solution` finds nothing, `main.py` falls back to `find_open_question`. That fallback attaches on word overlap of only 0.5 (`matching.ATTACH_MIN_SCORE`). Linking itself asks for 0.8 when the AI check is off (`linking.FALLBACK_MIN_SCORE`). On this path `linked` is None, so the email says "Your question was solved" instead of "possible answer". The open-question duplicate fallback also uses `best_match` at 0.3, so a new question can be merged into the wrong one and never saved. The chair confirmed this in the code.
- **Where:** `backend/main.py:405-417` and `435-446`, `backend/matching.py:11,61`, `backend/linking.py:82,778-797`.
- **How to fix:** Remove the fallbacks, because linking already covers fingerprints and words. Or make them use 0.8. Treat any text-match attach as `possible=True`. Only the explicit `open_question_id` path should say "solved".
- **Who:** backend.

### M3. The "possible answer" story is half-built
- **What is wrong:** A link made by similarity sets the question to solved for good. The website and search show it as fully solved, with no "possible answer" mark. If the fix is wrong, nothing reopens the question, because `worked=false` is only logged (`agent_db.py:257-258`). That also blocks the real answer from attaching later. `docs/linking.md:339-345` promises "worked 0 of 3 times", but failures are not stored. `plugin/mcp/server.py:648` says "the asker's agent verifies it", but nothing does. The hook only records an outcome for a viewed post when a command is already failing (`brain._note_viewed`). So an agent that follows the email in a fresh session usually records nothing. Part of this is the agreed trade-off of not adding a column.
- **Where:** `backend/main.py:283-288,411`, `backend/agent_db.py:257-258`, `docs/linking.md`, `plugin/mcp/server.py:648`, `plugin/hook/brain.py` (`_note_viewed`), `PROJECT_PLAN.md`.
- **How to fix:** Pick one path and write it in the Decisions log. Option (a): accept the limit and correct `linking.md` and the server.py wording. Option (b): add a small website hint with no new table. In both cases, have the email tell the user to run the failing command first, so the hook can see the fix pass.
- **Who:** backend and docs.

### M4. The re-run email points to a fix that is not on the post
- **What is wrong:** `POST /api/rerun` only sends the "possible answer" email. It never fills in the question, so the post still says "No solution yet". The fix lives only in the in-memory team report, which is lost when the server restarts. The email does not say to call `check_help` with the run id. Each re-run sends another email to the same people.
- **Where:** `backend/team_api.py:81-82,188-213`, `backend/accounts.py:226-233`.
- **How to fix:** Put the existing `message` text (team_api.py:195-197) into the email's `detail`, so it says "ask your agent to call check_help with run tr_xxx". Say that the report lasts only until the server restarts. Fix the matching sentence in `docs/demo-bugs.md:183-184` once its author is done.
- **Who:** backend.

### M5. A background or interrupted Bash run counts as a pass
- **What is wrong:** `bash_result` treats every PostToolUse Bash event as a pass unless an exit code says otherwise. Claude Code's Bash response usually has no exit code. A `run_in_background` command or a run the user interrupted fires right away with empty output. If that command had a pending KNOWN_FIX, the hook reports `worked=True`, which writes a real `post_useful` row and can trigger NOTABLE_SOLVE or LEAVE_REPLY. The chair confirmed there is no check for this.
- **Where:** `plugin/hook/notebook.py:286-303`.
- **How to fix:** Return None (record nothing) when `tool_input.run_in_background` is true, or when `tool_response` has `interrupted: true` or a `backgroundTaskId`. Add tests. Coordinate with the reply-nudge agent, which is editing `notebook.py`.
- **Who:** plugin.

### M6. The hook trusts post and post_open_question calls without reading their result
- **What is wrong:** The hook sets `oq_posted = True` after any `post_open_question` call, and closes the episode after any `post` call. The MCP tools return plain text for errors and for REVISE feedback, and Claude Code counts those as successful calls. If the server is down or the quality check asks for a rewrite, the hook still thinks the post worked. It never asks again, and the Stop reminder never fires.
- **Where:** `plugin/hook/brain.py` (`_after_mcp_tool`, about lines 352-370).
- **How to fix:** Read `notebook.tool_response_text(payload)`. Set `oq_posted` only when the text starts with "Posted as new open question" or "Already on the forum as open question". Close the episode only on "Posted as new solved post" or "Solution attached to open question". Add tests.
- **Who:** plugin.

### M7. The mock backend is behind the real API
- **What is wrong:** The mock's create-post answers lack `forum` and `tags`. `server.filed_under` reads both with `data[...]`, so the `post` and `post_open_question` tools crash with a KeyError against the mock. The README tells people to try the plugin this way. The mock also lacks register, visits, useful, replies and upvotes, and its search results lack `top_replies`, `reply_count`, `forum` and `visited_count`. So the reply flow cannot be shown on the mock. Smaller differences: the mock asks for the key on GET requests (the real API does not), and its report wording is slightly different. Note: the mock does NOT serve /api/updates. Its selftest expects a 404 (mock_backend.py:12 and 402-403). One reviewer said otherwise, and that was wrong.
- **Where:** `plugin/hook/dev/mock_backend.py`, `plugin/hook/dev/mock_data.json`, `plugin/mcp/server.py:224-231`.
- **How to fix:** Make the mock return `forum`, `tags` and `dropped_tags`. Add small in-memory versions of the missing routes, plus the new search fields. Also change `filed_under` to use `data.get(...)`, so a missing field can never crash a tool. Check the key on POST only.
- **Who:** plugin.

### M8. CORS only allows http://localhost:3000
- **What is wrong:** The allowed origin is hard-coded. Opening the site at 127.0.0.1:3000, on port 3001, or on a deployed URL blocks every API call. The forum then shows "can't reach the server".
- **Where:** `backend/main.py:37-42`.
- **How to fix:** Read a `CORS_ORIGINS` setting (comma-separated), with a default of localhost and 127.0.0.1 on port 3000. Include `WEBSITE_URL` as well. Document it with `NEXT_PUBLIC_API_URL` in the README and the testing guide.
- **Who:** backend.

### M9. Real emails may only reach the Resend account owner
- **What is wrong:** `RESEND_API_KEY` is set, but `EMAIL_FROM` is not. So mail goes out from `onboarding@resend.dev`. As far as the reviewer knows, that test sender only delivers to the Resend account owner. Email is now the only way people hear about answers. This is not verified.
- **Where:** `backend/mailer.py:36,376-386`, `backend/.env`.
- **How to fix:** Verify a sending domain in Resend and set `EMAIL_FROM`, or demo with the owner's address only. Do one real test send before the demo.
- **Who:** backend (setup).

### M10. Nothing links to the board room
- **What is wrong:** The nav has only Home, Forums and Docs. The landing page and /docs do not link to /board-room. The TEAM_STARTED text has no board-room URL. For the demo, someone has to type `/board-room/tr_...` by hand. "Open Question #N" in the board room is plain text, not a link.
- **Where:** `frontend/components/SiteHeader.tsx:4-8`, `frontend/app/page.tsx`, `frontend/components/board-room/BoardRoom.tsx:130`, `backend/team/gate.py`, `backend/team/report.py`.
- **How to fix:** Add a "Board room" tab to SiteHeader. Make "Open Question #N" a link to `/posts/N`. Optionally add `WEBSITE_URL/board-room/<run_id>` to the TEAM_STARTED message.
- **Who:** frontend (and backend for the message).

### M11. PROJECT_PLAN.md and the README disagree with what was built
- **What is wrong:** In PROJECT_PLAN.md, the my_updates row (line 30) says `/api/updates` "stays" and news arrives at session start. A later row says the opposite. The body sections (about lines 60, 183, 203, 219-232, 305-314, 333-341, 399-401, 618, 653) describe session-start updates, an old state-file path, missing tools and tables that do not exist. Every build-order box (lines 668-683) is unticked, though most items are done. The root README points to a missing CLAUDE.md and a missing `frontend/.env.example`. It uses the wrong MCP folder (`mcp` instead of `plugin/mcp`). It says the migration "must be applied", and it tells people to run DB tests that use the real database.
- **Where:** `PROJECT_PLAN.md`, `README.md`, `plugin/mcp/server.py:714` (stale "after an update names a post").
- **How to fix:** Tick the finished build-order items and strike out "SessionStart updates". Mark the my_updates row as superseded. Update the tables, the state path and moment 3 ("the asker and visitors get an email"). In the README, point to PROJECT_PLAN.md and the testing guide, fix the MCP folder, and say the database is already set up and nothing here changes tables.
- **Who:** docs.

### M12. Some opt-in tests create tables in the real Supabase database
- **What is wrong:** With `RUN_DATABASE_TESTS=1`, `test_usage_database.py` and `test_related.py` use the real `DATABASE_URL`. They create and drop a schema with tables in it. `backend/test_schema_alignment.py` sits outside `tests/`, so pytest never collects it, but a plain unittest run executes all three of its tests. One of them creates temp tables on the live database and may use up real post id numbers (not verified). `conftest.py` already does this safely, with `TEST_DATABASE_URL` and a check that refuses Supabase hosts.
- **Where:** `backend/tests/test_usage_database.py:25-45`, `backend/tests/test_related.py:38,74`, `backend/test_schema_alignment.py:77-91`, `README.md:52-60`.
- **How to fix:** Switch these tests to `TEST_DATABASE_URL` with the same Supabase guard. Build their tables from `tests/breakpoint_schema_stub.sql`. Move `test_schema_alignment.py` into `backend/tests/`, run it only when an opt-in setting is on, and keep only its read-only checks for the live database.
- **Who:** backend.

### M13. Dead voting code, and a vote route nothing calls
- **What is wrong:** `VoteColumn.tsx` has been imported nowhere since commit efb79c9. `lib/votes.ts`, `vote()` in `lib/api.ts:51-61` and the `VoteTarget` type exist only for it. The backend route `POST /api/posts/{id}/vote` has no caller, yet `docs/api-endpoints.md` lists it as a website route.
- **Where:** the frontend files above, `backend/main.py:480-495` (plus `VOTE_*`, `_votes_by_ip`, `allow_vote`), `docs/api-endpoints.md:36`.
- **How to fix:** The team decides whether votes stay. If not, delete them (see the removal list). If they stay, put VoteColumn back on the post page.
- **Who:** frontend and backend.

## Low

Plugin:
- **NOTABLE_SOLVE does not pass along the known open question.** The hook knows `episode['open_question_id']`, but `messages.notable_solve` is called without it. Linking then has to find the question, and the email says "possible" instead of "solved". Fix: save the id before clearing the episode, and add "pass open_question_id N to post" to the message. Files: `plugin/hook/brain.py`, `plugin/hook/messages.py`. (pending files; do after the reply work)
- **The hook reacts to any MCP tool and matches names loosely.** `mcp__.*` runs the hook for every MCP tool from every server. The suffix checks (`__post`, `__search`, `__reply`, `ask_for_help` anywhere in the name) mean another server's `post` tool can close an episode. Two reviewers reported this. Fix: match only `mcp__plugin_breakpoint_breakpoint__.*`, and accept the old agentoverflow prefix during the rename. Files: `plugin/hooks/hooks.json`, `plugin/hook/brain.py:352-370`.
- **The forum step looks only at the top search result.** The server gate looks at every result and prefers a solved one. Fix: pick the first close solved match, then the first close open match. Files: `plugin/hook/brain.py` (`_climb`), `backend/team/gate.py:144-163`.
- **The tester and board room expect a case file that GET /api/help/{id} never sends.** The main flow still works because the tester falls back to `--check`. Fix: send a small safe `case_summary` (goal, symptom, check command, environment), or remove the dead reads. Files: `plugin/tester/test_fixes.py:257`, `backend/team_api.py:152-162`, `frontend/lib/board-room/useLiveRun.ts:93`.
- **The tester can run longer than the 600 s Bash timeout.** If Bash kills it, cleanup never runs and worktrees are left behind. Also, the real `claude -p` flags (such as `--max-turns`) are never tested. Fix: give the run a time budget under 600 s, and do one real run before the demo. Files: `plugin/tester/test_fixes.py`, `plugin/hook/messages.py`.
- **One hook event can go over the 10 s hook timeout in slow repos, and parallel events can overwrite each other's notes.** Fix: give each event a total time budget, and optionally use a lock file. Files: `plugin/hook/checkpoints.py`, `plugin/hook/notebook.py`.
- **No tests for ask_for_help, check_help or the case-file builder.** This code sends local project content to the server. `SECRET_FILE_RE` does not catch `service-account.json` or `token.txt`. Fix: add tests with a temp git repo, and widen the regex. Files: `plugin/mcp/server.py`, `plugin/tests/test_server.py`.
- **The plugin-option email overwrites an email saved with /setup** at every session start. Fix: let a "setup" email win. Files: `plugin/hook/identity.py:297-303`, `plugin/commands/setup.md`.
- **Check once:** that `${CLAUDE_PLUGIN_ROOT}` is filled in inside `commands/setup.md`, and that `python` exists on Mac/Linux machines (everything calls `python`).
- **Stale comments and dead code:** `stuck_known_fix.jsonl` line 1 (talks about session-start news), `server.py:714`, `AGENT_NAME` (server.py:47, 687, and `plugin/.mcp.json`, which always gets overwritten), and `checkpoints.EMPTY_TREE` (line 38, unused).

Backend:
- **Unused or duplicate routes:** `GET /api/updates` (a stub that always returns [], `agent.py:152-159`), two registration routes doing one job (`POST /api/agents` in `activity.py:128-144` and `/api/agents/register` in `accounts.py:279-285`), and `GET /api/agents/{id}/public` (only tests call it). See the removal list.
- **Dead or duplicate helpers:** `linking.notify_possible_answer` (a no-op whose docstring points to a function that does not exist), `team_api.wait_for`, `matching.rank` (only a test uses it), the copied queries in `linking.py:160-194`, and three copies of `require_api_key` (main.py uses `!=`; the others use `compare_digest`).
- **Queries with no LIMIT:** `related_posts` and `find_open_question` read every post. `agent.py:97` calls `known_tags` once per tag. Fix: add LIMITs, and compute `known_tags` once. Files: `backend/main.py:262-298,440-443`, `backend/agent.py:97`.
- **No rate limit on the paid-AI routes** (`/api/judge`, `POST /api/posts`), and `_votes_by_ip` is never trimmed. This is fine for a hackathon.
- **Small crash risks:** `create_post` gives a 500 error if the "general" forum row is missing (`main.py:393-397`). The vote WHERE uses `solution` instead of `COALESCE(general_solution, solution)`.
- **Settings examples are stale.** `.env.example` lists unused `SUPABASE_*` and `LLM_MODEL`. It is missing `API_KEY`, `OPENAI_API_KEY`, `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_PROVIDER`, `WEBSITE_URL` and the optional `QUALITY_*`/`LINKING_*` settings. `agent_db.py:61` reads a leftover `V1_DB_POOL_SIZE`. Two reviewers reported this.
- **The migration file is older than the live schema.** `backend/migrations/20261024_post_usage.sql` does not match. The real copy of the schema is `tests/breakpoint_schema_stub.sql`. Mark the migration as history.
- **`pytest.ini` has `testpaths = tests`,** so the README's `pytest -q` skips `team/tests`. Also, 62 endpoint tests were skipped because no throwaway database was set up. Run them once with `TEST_DATABASE_URL`.
- **`backfill_embeddings.py` is named after a job it no longer does.** Rename it or delete it.

Website:
- **No `app/error.tsx` and no `app/not-found.tsx`.** One client error blanks the whole page. Add both.
- **Possible-answer links and team reports are not visible on the site,** and PROJECT_PLAN Box 3 lists pages that were never built (Stats, My questions). Update the plan table. This ties to M3.
- **Small leftovers:** there is no `test` script in `frontend/package.json` for `lib/usefulness.test.mjs`. `shortAgentId(post.author)` in `PostRow.tsx:61` and `PostView.tsx:69` does nothing now. The board room handles a "fallback" event the backend never sends (harmless).

Docs and repo:
- **`GET /api/posts/{id}/related` is missing from** `docs/api-endpoints.md` and `docs/backend-handoff.md`. Three reviewers reported this.
- **The testing guide is out of date.** It says "after main is merged" and "demo/ being built now". It leaves out `pytest plugin/tests` and `node --test frontend/lib/usefulness.test.mjs`.
- **`plugin/README.md` says useful marks are separate from hook outcomes.** Both actually write `post_useful`. Fix the wording.
- **`docs/backend-handoff.md` and `docs/database-proposal.md` repeat other docs.** database-proposal.md also has rejected CREATE TABLE examples that someone could mistake for steps to run.
- **The root `.mcp.json` gives every Claude session in this repo full read and write Supabase MCP access.** Add `read_only=true`, or move it to a personal config. This is a security item. It is low only because it needs a sign-in.
- **The docstring in `backend/team/tests/conftest.py:6-9` is stale.** It mentions a Postgres store that no longer exists.
- **`taxonomy.json` is only found when the whole repo is present** (`server.py:104-111`). Check what happens when the plugin is installed on its own.

## Pending (in progress)

These are not counted as bugs. Re-check them when the other agents finish.
- **Rename (AgentOverflow to Breakpoint):** `messages._header` still prints `[AgentOverflow: TAG]`. The skill folder and name are still `agentoverflow`. SKILL.md says `/agentoverflow:setup`. `notable_solve` says "see the agentoverflow skill". Many files show as modified, `Diagrams.tsx` among them. After the rename, grep `plugin/` for "agentoverflow", and keep only the deliberate old-name fallbacks (`AGENTOVERFLOW_*`, the `~/.agentoverflow` copy, `refs/agentoverflow`).
- **Hook test failures:** 1 to 5 failures in `test_rewind` and `test_tester`, which changed between runs. The causes are the `[Breakpoint:` tag in the tests and a `TempProject.fail()` helper being edited. Re-run once both agents are done.
- **Reply nudge:** LEAVE_REPLY is wired into messages, brain and SKILL, and it fired in a replay. It still needs a row in the tag table in `docs/hook-contracts.md` (section c). The MCP `search` tool output (`server.format_post`) does not show `top_replies` or `reply_count`. `server.py` is not in the reply agent's file list, so the team needs to decide this after the reply work lands. The mock backend also needs the new fields (see M7).
- **docs/demo-bugs.md** is being written. Lines 183-184 say a re-run attaches the answer to the question. It does not (see M4).
- **`docs/vision-and-next-steps.md`** is untracked. Commit it once it is final.

## Removal list

### Safe to delete now
| Path | Reason | Confidence |
|---|---|---|
| `frontend/public/file.svg`, `globe.svg`, `next.svg`, `vercel.svg`, `window.svg` | create-next-app starter icons. Nothing refers to them. The favicon is `app/icon.svg`. | high |
| `past_plan.md` | Old copy of PROJECT_PLAN.md with /v1, my_updates and run_specialists. It can mislead agents. Git keeps the history. | high |
| `questions.md` | Three early brainstorm questions, all answered now. | high |
| `plugin/hook/dev/mock_requests.log` | Local mock log (gitignored, rewritten on every run). | high |
| All `__pycache__/` folders, including stale `*_v1*.pyc`, `test_v1*.pyc` and `test_team_store_pg*.pyc` | Compiled leftovers (gitignored). | high |
| `backend/linking.py:907-913` `notify_possible_answer` and `backend/tests/test_linking.py:310-312` | No-op with no caller. Its docstring points to a function that does not exist. | high |
| `backend/team_api.py:257-266` `wait_for` | Never called. | high |
| `frontend/components/VoteColumn.tsx`, `frontend/lib/votes.ts`, `vote()` in `lib/api.ts`, `VoteTarget` in `lib/types.ts` | Not used since efb79c9. Delete them together with the vote route below if the team drops votes. | high (if votes are dropped) |

### Check with the team first
| Path | Reason | Confidence |
|---|---|---|
| `backend/main.py:480-495` vote route, `VOTE_*`, `_votes_by_ip`, its tests, and row 12 of `docs/api-endpoints.md` | No caller. Remove only if votes are dropped. | medium |
| `backend/agent.py:152-159` `GET /api/updates` stub and `test_agent_endpoints.py:269-274,355-356` | Retired. Nothing calls it. It is kept only for "older hooks". Remove the doc rows too. | medium |
| `backend/activity.py:128-144` `POST /api/agents` and the MCP fallback at `server.py:64-66` | Same job as `/api/agents/register`. | medium |
| `backend/matching.py:86-91` `rank` and its test | Only a test uses it. | medium |
| `.env.example` lines `LLM_MODEL` and `SUPABASE_*` | No code reads them. Rewrite the file instead. | medium |
| `docs/superpowers/` (6 plan and spec files) | Finished AI work plans. Copy the /related description into api-endpoints.md first, or move them to `docs/archive/`. | medium |
| `docs/backend-handoff.md` | Mostly repeats api-endpoints.md. Merge it in, or cut it down to an index. | medium |
| `docs/database-proposal.md` (history part, from about line 50) | A rejected proposal with CREATE TABLE examples. Keep the "What was actually done" part. | medium |
| `frontend/README.md` | Stock create-next-app text. Replace it with short real notes rather than deleting it. | medium |
| `frontend/lib/format.ts` `shortAgentId` | Dead once B1 is fixed. | medium |
| `plugin/hook/checkpoints.py:38` `EMPTY_TREE`; `AGENT_NAME` in `plugin/mcp/server.py` and `plugin/.mcp.json` | Unused or no effect. Wait for the rename agent, which is editing these files. | high, after the rename |
| `backend/team_api.py` `_State.notify_fn` and its `message` text | Only tests use them. Reuse the text for the M4 fix first. | low |
| `backend/accounts.py:288-296` `GET /api/agents/{id}/public` | Only tests call it. Keep it if author pages are planned. | low |
| `backend/test_schema_alignment.py` | Move it into `backend/tests/` behind an opt-in setting. Do not just delete it. | low |
| `backend/migrations/20261024_post_usage.sql` | Out of date. Delete only after the DB tests use the schema stub. | low |
| `backend/backfill_embeddings.py` | Nothing to backfill. Rename or delete it. | low |
| `backend/linking.py:160-194` copied queries | Import them from agent_db instead. This is a refactor, not a delete. | low |
| `frontend/app/forum/page.tsx` | A redirect to /forums that nothing links to. It is harmless, so keeping it is fine. | low |
| Old-name fallbacks (`AGENTOVERFLOW_*`, `~/.agentoverflow`, `refs/agentoverflow`) | Keep for now, while the rename settles. Remove after the hackathon. | low |

Keep: `frontend/AGENTS.md` (`next dev` writes it), `taxonomy.json` (used by the MCP server and seed_tags).

## Verified working

- The live database has exactly the expected tables and views. Every SQL column the code uses exists. No code creates tables at runtime. Nothing points at removed tables.
- All routes are under `/api`. There is no `/v1` in the backend, plugin or website code (only in the /docs page text). Every caller hits a route that exists.
- The hook never posts. It only calls search, outcomes, judge and register. There is no session-start updates call. `run_specialists` and `my_updates` are gone, and a test checks this.
- The ladder order works: forum, then rewind offer, then step back, then call team, then post open question (only after the team really ran).
- Tags match on both sides. The tags the server sends (KNOWN_FIX, TRY_FIRST, TEAM_STARTED/REPORT/NO_ANSWER/FAILED/WORKING) are the ones the hook reads. Every tag the hook prints has a SKILL.md section.
- The tester works only in temp git worktrees, and its WINNER / NO_WINNER / INCONCLUSIVE first line is read by the hook. Fixes are never auto-applied.
- "Fix worked" becomes a `post_useful` row. The "solved" email goes out once (`UPDATE ... WHERE solution IS NULL RETURNING`) to the asker and the visitors, minus the solver.
- Team runs and embeddings are in memory only.
- No public route leaks full agent IDs or emails.
- The website builds (11 routes) with no TypeScript errors and no ESLint issues. Its API calls match the backend fields, except the reply author (B1). The board room is self-contained, and its types match the backend.
- Generated files and `backend/.env` are gitignored, and `.env` was never committed. The stray `$HOME/.volta` folder was removed.
- All 5 replay scenarios ran against the mock with the expected tags in the expected order.

## Tests run and results

The chair did not re-run the tests. The chair read code to spot-check the findings. These are the reviewers' results:
- `python -m unittest discover -s plugin/hook/tests`: 151 to 164 tests. 1 to 5 failures or errors, which changed between runs. All come from the in-progress rename and test edits (pending).
- `backend\.venv\Scripts\python -m pytest backend/tests backend/team/tests -q`: 299 to 302 passed, 62 to 63 skipped (the database tests need `TEST_DATABASE_URL`).
- `backend\.venv\Scripts\python -m pytest plugin/tests -q`: 26 passed.
- `python plugin/hook/dev/mock_backend.py --selftest`: all 28 checks passed.
- `claude plugin validate ./plugin`: passed, with 1 warning (no author in plugin.json).
- Website: `npm run build` passed. `npx eslint .` found no issues. `node --test lib/usefulness.test.mjs`: 6 of 6 passed.
- Read-only live schema checks: 2 OK. There were no database writes and no paid AI calls.

## Suggested order of fixes

1. **Now (about 20 minutes):** B1 (reply list crash) and B2 (set API_KEY in both places, then send one request with the key to check it).
2. **Now (about 15 minutes, dashboard only):** H1. Turn off the legacy Supabase keys. Remove the unused `SUPABASE_SECRET_KEY` from the local `.env`. Make the root `.mcp.json` read-only.
3. **Quick backend fixes (about 1 hour):** M1 (scrub replies), M2 (fallback matchers), M8 (CORS), M4 (re-run email text), M9 (test email send).
4. **After the rename and reply-nudge agents finish (about 1 to 1.5 hours):** re-run the hook tests. Then H3 (PowerShell), M5 (background runs), M6 (read post results), the MCP name prefix, and the NOTABLE_SOLVE open question id.
5. **Website and docs (about 2 to 3 hours):** H2 (/docs rewrite), M10 (board-room link), the error pages, M11 (PROJECT_PLAN and README), the testing guide, the /related route docs, and the LEAVE_REPLY tag row.
6. **Cleanup (about 30 minutes):** delete the "safe now" list. Decide on votes (M13) and the "check first" items as a team.
7. **When there is time:** M3 (possible-answer decision), M7 (mock backend), M12 (DB tests off the live database), and the rest of the Low items.

Total: about 6 to 8 hours of work. Items 1 to 3 (under 2 hours) are enough for a working demo.
