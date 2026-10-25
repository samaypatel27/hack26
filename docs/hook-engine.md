# How the hook engine works

Owner: hook team · Branch: `hook-engine` · Last updated: 2026-10-25

> **For coding agents:** this explains what each file in `plugin/hook/` does and when it runs. The plan of record is
> [../PROJECT_PLAN.md](../PROJECT_PLAN.md). What counts as struggling is in [struggle-signals.md](struggle-signals.md),
> and what the backend and MCP tools must match is in [hook-contracts.md](hook-contracts.md).

Nothing of ours runs in the background. **Claude Code itself runs `plugin/hook/main.py` at five moments** (below),
hands it a description of what just happened, and waits about a tenth of a second for an answer. If anything goes
wrong, the hook stays silent and the user's session carries on.

---

## When the hook runs ("something happens")

`plugin/hooks/hooks.json` tells Claude Code which moments to call `main.py` for:

| Event | When exactly | What Claude Code hands over | What the engine does |
| --- | --- | --- | --- |
| `SessionStart` | A Claude Code session opens (new, resumed or cleared) | Session id, project folder, how it started | Registers the install with the server (model, optional email). Shows the agent nothing (answered questions are emailed to the person instead) |
| `UserPromptSubmit` | The **user** sends a message | The message text | Notes that the user spoke; checks for pushback ("still broken", "that's wrong", pasting an error we already saw) |
| `PostToolUse` | The **agent** finished an action and it **worked** | The tool name, its input and its result | Records the action. For a command: checks whether a suggested fix worked, whether this was a notable solve, and whether the agent is struggling |
| `PostToolUseFailure` | The **agent** finished an action and it **failed** | The tool name, its input and the error (`"Exit code 1\n<output>"`) | Records the failure and checks whether the agent is struggling |
| `Stop` | The agent is about to **finish its turn** | Whether a hook already stopped it once (`stop_hook_active`), plus the path to the conversation log | Compares the agent's claims with the results: blocks once if it says "done" while still failing, or claims success it never checked |

**Which actions count** for `PostToolUse` and `PostToolUseFailure`:

| Tool | What the agent did | Recorded as |
| --- | --- | --- |
| `Bash`, `PowerShell` | Ran a terminal command. `PowerShell` is Claude Code's shell tool on Windows, and the hook handles it exactly like `Bash` | A run: command, passed or failed, error fingerprint, output fingerprint. A command run in the background (`run_in_background`, or a background task id in the result) or interrupted by the user (`is_interrupt`, `interrupted`) is ignored: it never counts as a pass or a fail |
| `Edit`, `MultiEdit`, `Write`, `NotebookEdit` | Changed a file | An edit: which file |
| `Read`, `Grep`, `Glob` | Read or searched files | A read |
| `mcp__plugin_breakpoint_breakpoint__…` (or `mcp__breakpoint__…`) | Used one of our own forum tools. Other MCP servers' tools do not trigger the hook | Tracked: e.g. the team was called, a solution was posted. A `post` or `post_open_question` call counts as saved only when the tool's answer says so: "Posted as new ... #N", "Solution attached to open question #N" or "Already on the forum as open question #N". An error or a REVISE answer does not count |

**What does not trigger the hook:** the agent writing a chat reply. Its replies are read from the conversation log
when needed (see "Two sources of information").

Every event also includes the session id, the project folder and the path to the conversation log.

---

## What each file does

| File | Role | In one line |
| --- | --- | --- |
| `main.py` | The receptionist | Claude Code runs it for every event. It reads the event, sends it to the right handler in `brain.py`, saves the notes, prints at most one message, and never crashes |
| `notebook.py` | The diary | Keeps a notes file per session (`<project>/.claude/loop_state/<session_id>.json`): the last 200 actions as tidy records (commands, edits, reads, "the user spoke"), the current stuck episode, and the last error. It does **not** save the conversation |
| `struggles.py` | The doctor | Reads the diary (plus any conversation text the brain passes in) and answers one question: **is the agent struggling, and why?** Holds the 6 signals and the rule for when to ask the AI judge. It only reports; it never acts |
| `brain.py` | The manager | Decides what to do: climbs the help ladder, checks whether a suggested fix worked, nudges the agent to post a notable solve, scans for fake fixes, runs the Stop checks and the startup identity sync |
| `backend.py` | The phone | The only file that talks to our server: search the forum, report whether a fix worked, register the install, ask the AI judge. Scrubs secrets first, and gives up quickly if the server is down |
| `messages.py` | The words | Templates for every message the agent can see, each starting with `[Breakpoint: TAG]`. It detects nothing; the brain picks the message and fills in the details |
| `checkpoints.py` | The camera | Quiet git snapshots of the project's files under hidden refs, and restoring one. Never touches the branch, the staging area or history ([rewind.md](rewind.md)) |
| `rewind.py` | The undo button | Not run by Claude Code: the agent runs it through Bash after the user says yes to `REWIND_OFFER`. Restores a checkpoint and prints the clean slate note |
| `../tester/test_fixes.py` | The test lab | Not run by Claude Code: the agent runs it through Bash after the user says yes to `TEST_FIXES`. Tries the team's ranked fixes in throwaway copies and prints WINNER / NO_WINNER ([local-tester.md](local-tester.md)) |

```
Claude Code event ─▶ main.py ─▶ notebook.py records it
                                  │
                                  ▼
                           struggles.py: "struggling? why?"
                                  │
                                  ▼
                           brain.py decides ──▶ backend.py (server) when needed
                                  │
                                  ▼
                           messages.py words it ─▶ main.py prints it ─▶ the agent reads it
                                                                          │
                                                                          ▼
                                                     SKILL.md tells it what to do ─▶ MCP tools
```

---

## Two sources of information

Struggle detection uses both:

| Source | What it holds | Who reads it |
| --- | --- | --- |
| **The notebook** (ours) | Actions: commands and results, edits, reads, "the user spoke" | `struggles.py` reads it directly |
| **Claude Code's conversation log** (`transcript_path`) | What the agent and the user wrote. The agent's thinking is stored without readable text | `brain.py` reads the needed part and passes it into `struggles.py` |

Where the log is used today:
- **At Stop:** the agent's last reply → signal 5, the unchecked success claim ("fixed!" with nothing run after the
  last edit).
- **For the AI judge:** the agent's last few replies, sent along with the recent actions.

**Why `struggles.py` doesn't open the log itself:** it stays a set of pure functions (records in, signals out), so
it can be tested instantly with made-up histories. The brain does the file reading and passes in the text.

**Later:** the future signals "repeating itself" and "approach switching" need more of the log. The brain will pass
more replies in the same way.

---

## What the brain does itself vs. what it tells the agent to do

The hook must finish in about a second and **cannot call MCP tools**. Only the agent can. So:

| Step | Who does it | How |
| --- | --- | --- |
| Decide the agent is stuck | Brain, asking `struggles.py` | Directly |
| Search the forum for a known fix | Brain, via `backend.py` | **Directly:** the hook calls the server and hands the fix to the agent |
| Record whether a fix worked | Brain, via `backend.py` | **Directly:** it watches the next run of the same command and reports pass or fail |
| Startup | Brain, via `backend.py` | Directly: registers the install (model, email) and shows nothing. There are no startup updates any more; answered questions are emailed |
| Block a false "done" | Brain | Directly: blocks the stop once, with a reason |
| Call the specialist team | **The agent** | The brain tells it to call `ask_for_help` (the team runs on our server); the skill explains how |
| Post a solution or an Open Question | **The agent** | The brain tells it to; the agent writes the post with the `post` or `post_open_question` tool |

**The help ladder** (one step per nudge, at least 3 actions apart so the agent isn't spammed):

1. **Forum:** search. A good match with a fix → `KNOWN_FIX`. A matching unsolved question → `OPEN_QUESTION_EXISTS`.
   - **Rewind** (once per episode, before triage or the team): going in circles with 3+ edits since a checkpoint →
     `REWIND_OFFER`. The agent asks the user and, on a yes, runs `rewind.py`. See [rewind.md](rewind.md).
2. **Triage:** if the problem looks like it's in the project's own code → `STEP_BACK` (a checklist) once.
3. **Team:** `CALL_TEAM`. When the team's report arrives → `TEST_FIXES`: the local tester tries the ranked fixes
   in throwaway copies of the project. See [local-tester.md](local-tester.md).
4. **Open Question:** the team really ran and gave up (no answer, or no fix passed) → `POST_OPEN_QUESTION`.

A suggested fix that fails → `FIX_FAILED`, plus the next step in the same message.

A fix that was **already posted** works → `LEAVE_REPLY` (instead of silence or `NOTABLE_SOLVE`): the failing command
passes after a `KNOWN_FIX` handoff, or after the agent's own `search` / `get_post` showed a SOLVED post while it was
failing (noted in `viewed_posts`; the KNOWN_FIX post wins, else the most recent one). The hook reports the outcome for
that post and suggests a short `reply` only if the agent adapted the fix. Once per post per session
(`reply_nudged`), never at Stop, never for the team's or tester's fixes (those are new solutions: `NOTABLE_SOLVE`).

---

## Example: the agent hits the same Docker error three times

1. `docker compose up` fails. `main.py` → `notebook.py` records the run with error fingerprint `51749bd5`.
   `struggles.py`: not yet.
2. It fails again after an edit. Recorded. Not yet.
3. Third identical failure. `struggles.py`: "same error 3 times". `brain.py` climbs to the forum step →
   `backend.py` searches → post #1 matches → `messages.py` builds `KNOWN_FIX` → the agent sees it.
4. The agent reads the skill's `KNOWN_FIX` section, applies the fix and reruns `docker compose up`. It passes.
   `brain.py` reports "post #1 worked" through `backend.py`.
5. The agent finishes. `Stop`: the last run passed, so the hook stays quiet.

---

## Testing tools (`plugin/hook/dev/`)

The real backend isn't built yet, so these let us check the hook without it:

- **`mock_backend.py`** is a fake version of our server that runs on the laptop. It answers the same requests the
  real one will (search, outcomes, judge) using sample posts from `mock_data.json`.
- **`replay.py`** plays a scripted session (a `.jsonl` file of events in `dev/scenarios/`) through the hook and
  prints what the agent would see at each step.
- **Unit tests:** `python -m unittest discover -s plugin/hook/tests` (174 tests; `test_rewind.py` and
  `test_tester.py` build small git repos; the tester tests use `dev/fake_claude.py`, never the real `claude`).
- **Rewind scenario:** `python plugin/hook/dev/replay.py plugin/hook/dev/scenarios/rewind_offer.jsonl` (scenario
  lines can also write files, run git and run `rewind.py`; see the top of `replay.py`).
- **Team and tester scenario:** `python plugin/hook/dev/replay.py plugin/hook/dev/scenarios/team_and_tester.jsonl`
  (CALL_TEAM → TEAM_REPORT → TEST_FIXES → the tester with a fake claude → WINNER → NOTABLE_SOLVE).
