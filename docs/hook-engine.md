# How the hook engine works

Owner: hook team · Branch: `hook-engine` · Last updated: 2026-10-25

> **For coding agents:** this explains what each file in `plugin/hook/` does and when it runs. The plan of record is
> [../PROJECT_PLAN.md](../PROJECT_PLAN.md). What counts as struggling is in [struggle-signals.md](struggle-signals.md),
> and what the backend and MCP tools must match is in [hook-contracts.md](hook-contracts.md).

Nothing of ours runs in the background. **Claude Code itself runs `plugin/hook/main.py` at six moments** (below),
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
| `PostToolUseFailure` | The **agent** finished an action and it **failed** | The tool name, its input and the error (`"Exit code 1\n<output>"`) | Records the failure. For a command with an error this session has not looked up yet: asks the forum once, right away (the early forum check). Then checks whether the agent is struggling |
| `PreToolUse` | The **agent** is about to use any tool | The tool name and its input | Nearly always nothing: it only reads the notes file. While the stuck menu waits for the user, any tool except `AskUserQuestion` (or `ToolSearch` / `Skill`, to load it) is denied and the session ends with the menu as text (see "The stuck menu") |
| `Stop` | The agent is about to **finish its turn** | Whether a hook already stopped it once (`stop_hook_active`), plus the path to the conversation log | Compares the agent's claims with the results: blocks once if it says "done" while still failing, or claims success it never checked |

**Which actions count** for `PostToolUse` and `PostToolUseFailure`:

| Tool | What the agent did | Recorded as |
| --- | --- | --- |
| `Bash`, `PowerShell` | Ran a terminal command. `PowerShell` is Claude Code's shell tool on Windows, and the hook handles it exactly like `Bash` | A run: command, passed or failed, error fingerprint, output fingerprint. A command run in the background (`run_in_background`, or a background task id in the result) or interrupted by the user (`is_interrupt`, `interrupted`) is ignored: it never counts as a pass or a fail |
| `Edit`, `MultiEdit`, `Write`, `NotebookEdit` | Changed a file | An edit: which file |
| `Read`, `Grep`, `Glob` | Read or searched files | A read |
| `AskUserQuestion` | Asked the user a question | Not recorded. While the stuck menu waits, its result is the user's answer |
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
| `backend.py` | The phone | The only file that talks to our server: search the forum, report whether a fix worked, register the install, ask the AI judge, and start the team (`POST /api/help`) after the user picks it. Scrubs secrets first, and gives up quickly if the server is down. Also reads the `team_mode` setting |
| `case_builder.py` | The case file | Builds what `POST /api/help` gets: the problem plus the environment, package versions, short file excerpts and `git diff`. Shared with the MCP `ask_for_help` tool, so both send the same thing |
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
| Search the forum for a known fix | Brain, via `backend.py` | **Directly:** the hook calls the server and hands the fix to the agent. Once on the first failure of each new error (the early forum check), and again at the ladder's forum step |
| Record whether a fix worked | Brain, via `backend.py` | **Directly:** it watches the next run of the same command and reports pass or fail |
| Startup | Brain, via `backend.py` | Directly: registers the install (model, email) and shows nothing. There are no startup updates any more; answered questions are emailed |
| Block a false "done" | Brain | Directly: blocks the stop once, with a reason |
| Call the specialist team | Brain, via `backend.py`, after the user picks it (or by itself with `team_mode` auto) | **Directly:** the hook sends `POST /api/help` with the case file and tells the agent the run id (`TEAM_CALLED`). Only if that fails does the agent call `ask_for_help` itself (`CALL_TEAM`) |
| Post a solution or an Open Question | **The agent** | The brain tells it to; the agent writes the post with the `post` or `post_open_question` tool |

**The early forum check** (before the agent counts as stuck). A strong model often fixes a bug in 1 or 2 tries, so
it may never fail 3 times, and then the ladder below would never ask the forum. So when a command fails with an
error fingerprint that this session has not looked up yet, the hook searches the forum once, right away:

- The top result is a close match (same fingerprint, or a text score of at least 0.5) and it is a **solved** post
  → `KNOWN_FIX`, exactly as on the ladder: the next run of that command is the fix's test (outcome, `useful` mark,
  `LEAVE_REPLY`).
- It is an **open question** → `OPEN_QUESTION_EXISTS`, and the episode remembers its id (`open_question_id`). If
  the agent then makes the command pass, even after only 1 or 2 failed runs, it gets `NOTABLE_SOLVE`. That message
  names the open question, so the agent passes `open_question_id` to `post`.
- No close match, or the server is down → nothing is said, and everything goes on exactly as before.

A match opens the episode (if there is none) and counts as the ladder's forum step, so the ladder doesn't search
again. Each fingerprint is looked up only once per session (the notes file keeps a `looked_up` list; the ladder's
own search adds to it too). The check is skipped while an episode about another command is open, and right after
another message (the same cooldown as the ladder); in that case the error can still be looked up at its next failure.
If a fix from the early check fails while the agent is not stuck yet, it gets `FIX_FAILED` alone. The next ladder
step waits until the agent really counts as stuck.

**The help ladder** (one step per nudge; after a message the ladder waits 3 actions or 2 failed check runs,
whichever comes first, so the agent isn't spammed and gets a try at each step. One failure is enough when it
shows a different error than the last message was about (each fix reveals a new error, so the agent is going
in circles), and after a `REWIND_OFFER`, which is only a question for the user):

1. **Forum:** search (skipped when the early forum check already found a match). A good match with a fix →
   `KNOWN_FIX`. A matching unsolved question → `OPEN_QUESTION_EXISTS`.
   - **Rewind** (once per episode, before triage or the team): going in circles with 3+ edits since a checkpoint →
     `REWIND_OFFER`. The agent asks the user and, on a yes, runs `rewind.py`. See [rewind.md](rewind.md).
2. **Triage:** if the problem looks like it's in the project's own code → `STEP_BACK` (a checklist) once.
3. **Team:** what happens depends on the `team_mode` setting (below). With the default, `ask`, the user gets the
   stuck menu. When the team runs and its report arrives → `TEST_FIXES`: the local tester tries the ranked fixes in
   throwaway copies of the project. See [local-tester.md](local-tester.md).
4. **Open Question:** the team really ran and gave up (no answer, or no fix passed) → `POST_OPEN_QUESTION`.

A suggested fix that fails → `FIX_FAILED`, plus the next step in the same message.

**The `team_mode` setting.** The environment variable `BREAKPOINT_TEAM_MODE`, else the `"team_mode"` key in
`~/.breakpoint/config.json`. Any other value (like the old `"local"`) counts as `ask`.

| Value | When the agent is stuck and the forum has no fix |
| --- | --- |
| `ask` (default) | One `STUCK_MENU`, once per episode, in place of `REWIND_OFFER` and the team step |
| `auto` | No menu. At the team step the hook starts the team itself (`TEAM_CALLED`). `REWIND_OFFER` works as before |
| `off` | The team is never started or suggested. `REWIND_OFFER` works as before |

**The stuck menu** (`team_mode` ask). Where the ladder would send `REWIND_OFFER` or the team step, it sends
`STUCK_MENU` instead: "call `AskUserQuestion` now" with these options:

1. "Call the specialist team"
2. "Rewind the files to before the problem started" (only when a rewind is possible, by the same rules as
   `REWIND_OFFER`)
3. "Let the agent keep trying"

The question names the failing command, how many times it failed and how many different errors it showed. The notes
file keeps the menu (`pending_menu`) until it is answered:

- **The agent uses another tool first:** the `PreToolUse` hook denies that tool and ends the session
  (`{"continue": false, "stopReason": ...}`). The user sees the menu as text: "Type 1, 2 or 3".
- **The agent tries to finish without asking:** the Stop hook blocks once and asks it to show the menu (with
  `AskUserQuestion`, or as text word for word if that tool is missing). It is skipped if the reply already shows it.
- **The answer** comes from the `AskUserQuestion` result (the `answers` field, or the `"question"="answer"` text),
  or from the user typing `1`, `2`, `3` or the option's words as their next message. Any other message drops the menu.
- **Team:** the hook sends `POST /api/help` itself, with the same case file `ask_for_help` builds (`case_builder.py`):
  the command, the error, what was tried, the fingerprint, up to 5 recently edited files, the environment and
  `git diff`. On `TEAM_STARTED` it marks the episode exactly as an `ask_for_help` reply would (team ran, run id,
  status "working") and sends `TEAM_CALLED`: call `check_help` with that run id. So `TEST_FIXES` and
  `POST_OPEN_QUESTION` work as before. If the request fails → `CALL_TEAM`, so the agent calls `ask_for_help` itself.
- **Rewind:** `REWIND_OFFER`, saying the user already said yes, so the agent runs the "when this problem started"
  option's command.
- **Keep trying:** nothing more this episode until the next step (the open question) is due.

In headless runs (`claude -p`) `AskUserQuestion` does not exist, so the agent shows the menu as text and the user
answers by typing a number in the next prompt.

A fix that was **already posted** works → `LEAVE_REPLY` (instead of silence or `NOTABLE_SOLVE`): the failing command
passes after a `KNOWN_FIX` handoff, or after the agent's own `search` / `get_post` showed a SOLVED post while it was
failing (noted in `viewed_posts`; the KNOWN_FIX post wins, else the most recent one). The hook reports the outcome for
that post and suggests a short `reply` only if the agent adapted the fix. Once per post per session
(`reply_nudged`), never at Stop, never for the team's or tester's fixes (those are new solutions: `NOTABLE_SOLVE`).

---

## Compound shell commands

Agents often chain commands in one shell call, for example a patch script and the tests together:

```
cd app && python - <<'EOF'
p = 'src/store.py'; s = open(p).read(); open(p, 'w').write(s.replace('a', 'b'))
EOF
python -m unittest 2>&1 | tail -20; git status --short
```

`struggles.split_command` splits a call on newlines, `;`, `&&`, `||` and `|` (outside quotes) and skips heredoc
bodies (`<<'EOF' ... EOF`, PowerShell `@' ... '@`). Then:

- **The key** (`command_key`) is the **check** in the call: a test or build runner (`python -m unittest/pytest`,
  `pytest`, `npm test/run`, `node --test`, `npx`, `tsc`, `cargo`, `go test`, `make`, ...). The call above is keyed
  `python unittest`, the same as a plain `python -m unittest`. Without a check, the key is the last part that is not
  a probe (`git status/diff/log`, `ls`, `cat`, `echo`, `cd`, `pwd`, `type`, `dir`, `Get-*`, ...). A probe never
  becomes the key when a check is present.
- **Pass or fail** (`notebook.check_result`): the exit code belongs to the last part. If the check is the last part
  (or only piped into `tail`/`head`), the exit code counts as before. Otherwise the check's own summary decides
  (unittest `FAILED (` / `OK`, pytest `N failed` / `N passed`, cargo/go/jest summaries, `npm ERR!`, then a
  traceback or `Error:` line). `check && other` exiting 0 is a pass. If the output doesn't say, the run is
  **unknown and not recorded**: never a pass.
- **Edits through the shell** (`notebook.shell_edits`): a call that writes files also records an `edit` (with
  `"via": "shell"`) for each file, before its run: `python - <<EOF` / `python -c` scripts that open files for
  writing (`open(p, 'w')`, `Path(x).write_text`), `sed -i`, `Set-Content` / `Add-Content` / `Out-File`, `>` / `>>`
  redirects (also `cat > file <<EOF`) and `tee`. `/dev/null` and `$null` don't count. So `REWIND_OFFER` and the
  "edit, run, same output" signal work for agents that patch through the shell.
- **Stop:** `NOT_DONE` ignores a failed run that a later passing check answers: the same check, a wider run of the
  same runner (`python -m unittest` passing covers `python -m unittest tests.test_store`), or, for a failed command
  that is not a check (a patch script, a debug snippet), any later passing check.

## Example: the agent hits a Docker error the forum already knows

1. `docker compose up` fails. `main.py` → `notebook.py` records the run with error fingerprint `51749bd5`.
   This session has not looked up `51749bd5` yet, so `brain.py` does the early forum check → `backend.py`
   searches → post #1 matches → `messages.py` builds `KNOWN_FIX` → the agent sees it.
2. The agent reads the skill's `KNOWN_FIX` section, applies the fix and reruns `docker compose up`. It passes.
   `brain.py` reports "post #1 worked" through `backend.py`, then sends `LEAVE_REPLY`.
3. The agent finishes. `Stop`: the last run passed, so the hook stays quiet.

If the forum had nothing, step 1 would stay quiet. After the third identical failure, `struggles.py` says "same
error 3 times" and `brain.py` climbs the ladder (the forum search again, then triage, then the team).

---

## Testing tools (`plugin/hook/dev/`)

The real backend isn't built yet, so these let us check the hook without it:

- **`mock_backend.py`** is a fake version of our server that runs on the laptop. It answers the same requests the
  real one will (search, outcomes, judge) using sample posts from `mock_data.json`.
- **`replay.py`** plays a scripted session (a `.jsonl` file of events in `dev/scenarios/`) through the hook and
  prints what the agent would see at each step.
- **Unit tests:** `python -m unittest discover -s plugin/hook/tests` (220 tests; `test_rewind.py` and
  `test_tester.py` build small git repos; the tester tests use `dev/fake_claude.py`, never the real `claude`).
- **Rewind scenario:** `python plugin/hook/dev/replay.py plugin/hook/dev/scenarios/rewind_offer.jsonl` (scenario
  lines can also write files, run git and run `rewind.py`; see the top of `replay.py`).
- **Team and tester scenario:** `python plugin/hook/dev/replay.py plugin/hook/dev/scenarios/team_and_tester.jsonl`
  (CALL_TEAM → TEAM_REPORT → TEST_FIXES → the tester with a fake claude → WINNER → NOTABLE_SOLVE). With the default
  `team_mode` (ask) the team step now shows `STUCK_MENU` where the scenario was written for `CALL_TEAM`.
