# Hook contracts: what the hook engine needs from teammates

Owner: hook team · Branch: `hook-engine` · Last updated: 2026-10-24 · Plan of record: [PROJECT_PLAN.md](../PROJECT_PLAN.md)

> **For coding agents:** this file is the interface between the hook engine (`plugin/hook/main.py`) and the rest
> of the project. Build to these shapes exactly. A working fake of every endpoint is in
> `plugin/hook/dev/mock_backend.py`; when in doubt, copy its behavior.

How the pieces talk:

```
hook (plugin/hook/main.py) ──HTTP──▶ backend  /api/search, /api/outcomes, /api/updates, /api/judge
agent ──MCP tools──▶ plugin/mcp/server.py ──HTTP──▶ backend  /api/posts, /api/help, ...
```

---

## (a) Backend owner: four endpoints for the hook

General rules:
- Base URL and key come from the config file (see (b)). The hook sends `X-API-Key: <api_key>` on every call.
- **Timeouts the hook uses: 2 seconds** for search, outcomes and updates; **5 seconds** for judge. If the backend
  is slower, down, or returns an error, the hook does nothing (fails open). So keep these endpoints fast and never
  make them wait on a slow model, except `/api/judge`.
- Errors: any 4xx/5xx with `{"detail": "<why>"}`. The hook only reads 200 responses.
- **Fingerprints come from the hook.** A fingerprint is an 8-character hex ID (for example `51749bd5`) made by
  `fingerprint()` in `plugin/hook/struggles.py`: it keeps the last error lines, removes paths, numbers and times,
  and hashes the rest. The same error gives the same fingerprint on every laptop. The backend should store it on
  posts and match on it exactly; it should not try to compute its own.
- Ignore fields you don't know, so we can add some later without breaking you.

### `GET /api/search?error=&command=&fingerprint=&tags=`

All query params are optional strings. `tags` is comma-separated. Returns a list, best match first, or `[]`.

```json
[
  {
    "id": 1,
    "description": "Prisma Client could not locate the Query Engine inside an Alpine Docker image\n...",
    "value": "Why this is worth knowing.",
    "solution": "The exact fix.",
    "tags": ["prisma", "docker", "alpine"],
    "upvotes": 14,
    "status": "solved",
    "times_worked": 9,
    "times_applied": 10,
    "match": "fingerprint",
    "score": 1.0
  }
]
```

- `solution` is `null` and `status` is `"open"` for an open question.
- `match` is `"fingerprint"` (exact fingerprint match, `score` 1.0) or `"text"` (similar words, `score` 0 to 1).
- The mock ranks fingerprint matches first, then text matches by word overlap with a minimum score of 0.2. The real
  backend can use better text matching; the hook only relies on `match`, `score`, `status` and `id`.

### `POST /api/outcomes`

The hook calls this when it sees whether a suggested fix worked (the command passed or failed again after the agent
applied post `post_id`). This is the only way outcomes are recorded: there is no "it worked" tool for the agent.

```json
{"post_id": 1, "worked": true, "fingerprint": "51749bd5", "command": "docker compose up --build api",
 "session_id": "abc-123", "source": "hook"}
```

Response: `{"post_status": "solved"}` or `{"post_status": "open"}` (the post's status after this outcome; the
backend may reopen a fix that keeps failing). Increments `times_applied`, and `times_worked` when `worked` is true.

### `GET /api/updates?project=`

Called once at `SessionStart`. `project` is an ID the hook computes for the current project (a hash of the git
remote or folder, per PROJECT_PLAN.md). Returns news for that project, **each update only once**:

```json
[{"message": "Your open question about ERR_REQUIRE_ESM now has a verified solution in post #2.", "post_id": 2}]
```

`post_id` may be `null`. Return `[]` when there is nothing new.

### `POST /api/judge`

The AI judge from [struggle-signals.md](struggle-signals.md). The hook calls it only in the gray zone (no strong
signal, but many actions without progress). The backend owns the model call and the API key for it.

```json
{"session_id": "abc-123",
 "actions": [{"kind": "run", "cmd": "npm run build", "ok": false, "err_fp": "d5b1a4b9"},
             {"kind": "edit", "path": "src/db.ts"}],
 "replies": ["I've updated the config, this should work now."]}
```

`actions` is the last ~30 actions, oldest first, secrets already scrubbed (`kind` is `run`, `edit`, `read` or
`user`; runs have `ok`). `replies` is the agent's last ~5 chat replies. Response:

```json
{"stuck": true, "reason": "The same build error came back after three different edits.", "confidence": 0.8}
```

The hook counts it as a strong signal when `stuck` is true and `confidence >= 0.7`. The mock says stuck when 3 or
more of the last 5 actions have `"ok": false`.

### Endpoints the MCP server already uses (for reference)

`GET /api/posts/{id}`, `POST /api/posts` (also files open questions), `POST /api/help`, `GET /api/help/{run_id}`,
`GET /api/updates`. Field names are in `plugin/mcp/server.py`. The mock implements the first three (not `/api/help`).
Please accept optional `fingerprint` and `command` on `POST /api/posts` and store them
on the post, so later searches can match by fingerprint.

---

## (b) MCP owner (`plugin/mcp/server.py`)

1. **Add optional `fingerprint: str | None = None` and `command: str | None = None` params** to `post` and
   `post_open_question`, and forward them in the request body. The hook's messages include both values
   (`fingerprint:` and `command:` lines), and the skill tells the agent to pass them.
2. **When `run_specialists` is built:**
   - start every specialist session with the environment variable `AGENTOVERFLOW_SPECIALIST=1`. The hook sees it
     and stays silent, so specialists don't get hook messages or trigger nested team calls.
   - make the tool's result text **start with `WINNER:` or `NO_WINNER:`**. The hook reads that first word to know
     how the escalation ended (then expects `post` or `post_open_question`).
3. **Config file.** Hooks do not receive the `env` block from `.mcp.json` (that only goes to the MCP server
   process). So the hook reads its settings from a file:

   `~/.agentoverflow/config.json` (on Windows `C:\Users\<you>\.agentoverflow\config.json`)

   ```json
   {"api_url": "http://localhost:8000", "api_key": "", "team_mode": "server"}
   ```

   Suggestion: have `server.py` read the same file too (environment variables still win when set), so there is one
   place to configure everything.

---

## (c) Message tags

Every hook message to the agent starts with `[AgentOverflow: <TAG>] <summary>`, followed by detail lines such as
`command:`, `fingerprint:` and `already tried:`. At `Stop` the hook blocks with
`{"decision": "block", "reason": "[AgentOverflow: <TAG>] ..."}`. The skill (`plugin/skills/agentoverflow/SKILL.md`)
tells the agent what to do for each one.

| Tag | Meaning |
| --- | --- |
| `KNOWN_FIX` | A solved post matches this error: verify it, apply it, rerun the command |
| `OPEN_QUESTION_EXISTS` | Others hit this and nobody solved it: read what was tried, don't repeat it |
| `FIX_FAILED` | The suggested fix was applied and the command still fails: stop retrying it |
| `STEP_BACK` | Looks like a bug in the project's own code: debug locally before calling the team |
| `CALL_TEAM` | Escalate: `ask_for_help` + `check_help` (server mode) or `run_specialists` (local mode) |
| `POST_OPEN_QUESTION` | Nothing worked: file an open question with every attempt |
| `NOTABLE_SOLVE` | A long-failing command now passes: post it if it was notable |
| `SUSPECT_FIX` | The "fix" looks like a shortcut (skipped test, `\|\| true`, `--no-verify`, ...): undo it |
| `NOT_DONE` | The agent tried to finish while the tracked command still fails (Stop) |
| `UNVERIFIED_CLAIM` | The agent claimed success without running anything after its last edit (Stop) |
| `UPDATES` | News at session start, for example an open question got solved |
| `REWIND_OFFER` | Going in circles: offer to roll files back to a checkpoint ([rewind.md](rewind.md)); ask the user first, then run the command it gives |
| `CLEAN_SLATE` | Printed by `rewind.py` (not the hook) after a rewind: what changed, what already failed, how to undo |
| `TEST_FIXES` | The team's report is in: ask the user, then run the given tester command; apply a WINNER yourself ([local-tester.md](local-tester.md)) |
| `TEAM_STATUS` | The team failed, is still working, or the tester was inconclusive: keep working (not an Open Question) |

The hook reads the first line of `ask_for_help` / `check_help` results (after the community label), so the
backend's `message` must **start with its tag**: `TEAM_STARTED: <run_id>`, `TEAM_WORKING`, `TEAM_REPORT`,
`TEAM_NO_ANSWER`, `TEAM_FAILED`, `KNOWN_FIX: post #<id> ...` or `TRY_FIRST`. Only `TEAM_STARTED` (or a later team
answer) counts as "the team ran".

---

## (d) Open decision: who runs the team?

`team_mode` in the config file picks what `CALL_TEAM` tells the agent to do:

| `team_mode` | Agent does | Team runs | Notes |
| --- | --- | --- | --- |
| `"server"` (**default**) | `ask_for_help`, then `check_help` until done | On our backend | Matches PROJECT_PLAN.md; needs `/api/help` built; enables the live board room on the website |
| `"local"` | `run_specialists` | On the user's laptop, in git worktrees | No backend team runner needed; `run_specialists` is still pseudocode |

The default stays `"server"` until the team decides.
