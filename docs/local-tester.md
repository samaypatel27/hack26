# The local tester: proving the team's fixes in throwaway copies

Owner: hook team · Branch: `hook-engine` · Status: **built and tested** · Last updated: 2026-10-24 ·
Plan of record: [../PROJECT_PLAN.md](../PROJECT_PLAN.md) ("The backup team", member "Local tester")

> **For coding agents:** the tester is `plugin/tester/test_fixes.py`. The hook never runs it: it sends `TEST_FIXES`
> with the exact command, the agent asks the user, and on a yes runs it with Bash. It only ever works in copies of the
> project and **never changes the real project**. The agent applies the winning change itself. Names after the rename
> to Breakpoint: `BREAKPOINT_SPECIALIST`, `BREAKPOINT_TESTER_CLAUDE`, `[Breakpoint: TAG]`. The old `AGENTOVERFLOW_*`
> env names still work for now.

---

## What it does, in one paragraph

The backup team (on our server) researches a problem and sends back a ranked list of likely causes, each with exact
steps. Instead of the stuck agent trying them one by one in the user's project (each try leaving a mess), the tester
makes one **throwaway copy** of the project per hypothesis, has a headless Claude Code session (Sonnet) apply **only
that hypothesis's steps** in its copy, then **reruns the check command itself** to see which one really works. It
reports the winner and its exact changes. The agent then applies that change in the real project, where the user can
see it, and the hook confirms it by watching the check pass.

Why: the team's research is a guess until something runs. The tester turns "likely cause #2" into "this diff made
`npm test` pass", in parallel, without touching the user's files.

---

## The flow

```
CALL_TEAM ─▶ agent calls ask_for_help ─▶ "TEAM_STARTED: run-1"      (hook: team_ran = True, run id saved)
                agent keeps working, later calls check_help ─▶ "TEAM_WORKING ..."   (nothing to do)
                                              check_help ─▶ "TEAM_REPORT: run-1 ..."
hook ─▶ [Breakpoint: TEST_FIXES] + the exact tester command
agent ─▶ asks the user ("may I test the fixes in copies? a few minutes, uses Sonnet")
   no  ─▶ agent tries the ranked fixes itself, in order, rerunning the check after each
   yes ─▶ agent runs: python ".../plugin/tester/test_fixes.py" --project "..." --session <id> --run-id run-1 --check '<cmd>'
            (Bash, foreground, timeout 600000; Claude Code's permission prompt is a second yes)
tester ─▶ GET /api/help/run-1 (the report) ─▶ one copy per hypothesis (up to 3, in parallel) + one untouched baseline copy
            each copy: claude -p applies the steps ─▶ the tester captures the diff ─▶ the tester reruns the check
         ─▶ prints "WINNER: hypothesis 2 (<cause>)" + the diff, or "NO_WINNER: 3 hypotheses tried", or "INCONCLUSIVE: ..."
hook (sees the tester's Bash result; it is not counted as a struggling run)
   WINNER       ─▶ notes team_fix in the episode, says nothing
                   agent applies the diff by hand in the real project ─▶ reruns the check ─▶ passes
                   ─▶ hook closes the episode ─▶ NOTABLE_SOLVE "solved with the team's hypothesis 2 ... credit the team"
   NO_WINNER    ─▶ POST_OPEN_QUESTION (the agent posts with post_open_question)
   INCONCLUSIVE ─▶ TEAM_STATUS: try the ranked fixes yourself
Other team replies: TEAM_NO_ANSWER ─▶ POST_OPEN_QUESTION.  TEAM_FAILED ─▶ TEAM_STATUS (keep working, not an Open
Question).  KNOWN_FIX / TRY_FIRST ─▶ no team run (a KNOWN_FIX "post #N" is watched like the hook's own KNOWN_FIX).
```

The hook never posts and never runs the tester. It only tells the agent which tool or command to use.

---

## Files

| File | New or changed | Role |
| --- | --- | --- |
| `plugin/tester/test_fixes.py` | **new** | The tester script (standard library only). Everything described below |
| `plugin/hook/messages.py` | changed | New tags `TEST_FIXES` and `TEAM_STATUS`; `tester_command()` builds the exact command (absolute script path, project, session, run id, check); `CALL_TEAM` mentions the tester step; `NOTABLE_SOLVE` can credit the team |
| `plugin/hook/brain.py` | changed | Parses `ask_for_help` / `check_help` results (`TEAM_*`, `KNOWN_FIX`, `TRY_FIRST`) after stripping the community label; recognizes the tester command in Bash and reads its first line; the Open Question rule now needs a real team run that gave up; a direct `ask_for_help` is tied to the command failing right now |
| `plugin/hook/notebook.py` | changed | New episode fields (`team_run_id`, `team_status`, `team_report_idx`, `tester_offered`, `team_fix`); MCP results shaped `{"content": [...]}` are read as text |
| `plugin/skills/breakpoint/SKILL.md` | changed | Sections "Team replies", `TEST_FIXES`, `TEAM_STATUS`; `CALL_TEAM` shortened |
| `plugin/hook/dev/mock_backend.py`, `mock_data.json` | changed | Fake `POST /api/help` (`KNOWN_FIX` for a known fingerprint, else `TEAM_STARTED: run-1`) and `GET /api/help/{run_id}` (first `TEAM_WORKING` with events, then `TEAM_REPORT` with a canned 3-hypothesis report); `--selftest` checks them |
| `plugin/hook/dev/fake_claude.py` | **new** | A stand-in for `claude -p` for tests and the replay: applies scripted edits per hypothesis and logs what it saw |
| `plugin/hook/dev/replay.py` | changed | New scenario lines `_env` and `_run` (run a command like the agent's Bash and feed the result to the hook) |
| `plugin/hook/dev/scenarios/team_and_tester.jsonl` | **new** | The whole flow end to end with a fake claude |
| `plugin/hook/dev/scenarios/stuck_no_fix.jsonl` | changed | Uses the real tag format (`TEAM_STARTED`, `TEAM_REPORT`) |
| `plugin/hook/tests/test_tester.py` | **new** | 29 tests: tag parsing, TEST_FIXES contents, direct `ask_for_help`, tester command recognition, WINNER / NO_WINNER / INCONCLUSIVE, and the script on real temp git repos with the fake claude |
| `plugin/hook/tests/test_engine.py` | changed | Two tests used untagged replies ("Team started."); they now use `TEAM_STARTED` / `TEAM_REPORT` / `TEAM_NO_ANSWER` |
| `docs/hook-engine.md`, `docs/hook-contracts.md` | changed | Pointers to this doc, the new tags, and "the backend's `message` must start with its tag" |

---

## How the tester works

**1. The report.** `--run-id run-1` fetches `GET /api/help/run-1` with the hook's own config and request helper
(`backend.py`: `~/.breakpoint/config.json` or `BREAKPOINT_API_URL`). The shape is the one in
[api-endpoints.md](api-endpoints.md) (`report.hypotheses[]` with `rank`, `cause`, `confidence`, `steps`, `check`).
`--report file.json` reads the same JSON from a file instead (for tests and demos). The top `--max-hypotheses`
(default 3), by rank, are tested.

**2. The check.** One check for every hypothesis, so they are compared fairly. First match wins: `--check` (the hook
always passes the episode's failing command), the report's `check_command`, the case file's, the hypotheses' own
`check.command`, the session's last failing command. **No check anywhere:** a first session in its own copy writes one
small test that reproduces the symptom (from the case file, the team's check descriptions and the session's last
error) and ends its reply with `CHECK: <command>`. The tester runs it in an untouched copy: it must **fail** there,
or the run stops (INCONCLUSIVE). That test is then copied into every hypothesis copy, the sessions are told not to
touch it, a changed test marks the hypothesis as suspect, and it is left out of the diffs.

**3. The copies.** `git worktree add --detach <system temp>/breakpoint_tester_h<n>_*/copy <commit>`, outside the
project folder. `<commit>` is the session's **`episode_start` checkpoint** (from `checkpoints.py`, the one rewind uses:
"the files when this problem started", uncommitted and new files included, ignored files and `.claude/` left out). If
there is none: a fresh checkpoint of the files now (label `tester_start`), then `HEAD` (the output says which). The
user's git hooks are switched off for the worktree add (`core.hooksPath` = an empty folder), and the user's branch,
index and stash are never touched (worktrees have their own index).

**4. Dependencies (judgment call).** Ignored heavy folders are not in a checkpoint. By default (`--deps link`) the
tester links `node_modules`, `.venv` and `venv` (plus `--link NAME`) from the real project into each copy: a Windows
**directory junction** (no admin rights needed) or a symlink elsewhere. Only folders git ignores are linked. Links are
fast (milliseconds) but shared, so:
- the session is told never to modify a shared link, and it cannot run commands anyway (see 5);
- when a hypothesis's steps need an install, that copy gets a **private copy** of the folders instead (`shutil.copytree`,
  slower), so an install can never reach the real `node_modules`;
- links are removed **before** the copy is deleted, and if a link cannot be removed the copy is left in place rather
  than risk deleting through it.
`--deps copy` always copies, `--deps none` gives nothing, and `--setup "npm ci"` runs a command in each copy first.

**5. The session.** For each hypothesis, headless Claude Code runs in the copy, with the prompt on stdin:

```
claude -p --model sonnet --max-turns 30 --output-format json
       --permission-mode acceptEdits --permission-prompts none
       --strict-mcp-config --no-session-persistence
       --disallowedTools WebFetch WebSearch
       [--allowedTools "Bash(<exact cmd>)" "PowerShell(<exact cmd>)" ...]
```

Checked against `claude --help` (Claude Code 2.1.283) and with real calls:
- `acceptEdits`: file edits **inside the working directory** (the copy) are allowed without a prompt. Anything that would
  need a prompt is denied at once by `--permission-prompts none` (never a hang). This is the least permissive mode that
  still lets it edit; `bypassPermissions` / `--dangerously-skip-permissions` are never used.
- Commands: none, except the **exact** backticked commands in the steps that start with a project-local package
  manager (`npm`, `npx`, `pnpm`, `yarn`, `bun`, `cargo`, `go`, `dotnet`, `bundle`, `composer`) and contain no shell
  operators. pip/conda are never allowed (they usually change a shared Python). Both the Bash and PowerShell tools get
  the rule, because on Windows the model sometimes picks PowerShell. Verified: an allowed exact command ran, an unlisted
  one (`npm config get registry`) was denied instantly. Claude Code still auto-allows its own read-only commands (like
  `node --version`).
- `--strict-mcp-config` with no MCP config: no MCP servers, so a tester can never post to the forum or call the team.
- `--max-turns` is accepted by `-p` although `--help` does not list it (verified).
- Env: `BREAKPOINT_SPECIALIST=1` (our hook stays silent there, so there is no chain reaction), and the parent
  session's variables (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, ...) are removed.
- The prompt: apply ONLY this hypothesis's steps, work only in this directory, never weaken tests or the check, don't
  run the check, no installs or network unless listed, reply with one short paragraph.
- `--timeout` (default 180 s) per session; on timeout the whole process tree is killed (`taskkill /T /F` on Windows).

**6. Never trust the session.** After it stops, the tester (a) takes the change set: `git add -A` in the copy's own
index and `git diff --cached <start commit>` (new files included, `.claude/` excluded), **before** the check, so caches
the check writes stay out; (b) scans it with the hook's fake-fix detector (`|| true`, skipped tests, `except: pass`,
deleted tests); (c) reruns the check itself in the copy (`--check-timeout`, default 120 s; with Git Bash on Windows,
like the agent's Bash tool). Statuses: `passed`, `failed`, `suspect` (passed but hides the failure), `no_changes`
(passed with nothing changed), `no_steps`, `error`. The winner is the best-ranked `passed` one.

**7. Baseline.** One more untouched copy runs the check at the same time. If it **already passes** there, the copies
cannot tell a fix from no fix (the problem is in this machine's setup, an ignored file such as `.env`, or a running
service), and the result is `INCONCLUSIVE`. `--no-baseline` skips it.

**8. Cleanup, always.** In a `finally` for every copy: links removed, `git worktree remove --force --force` (retried for
Windows file locks), the temp folder deleted, then `git worktree prune`. Also on errors, timeouts and Ctrl+C.
`--keep` keeps them for debugging.

### Output

The first line is for the hook; the rest is for the agent:

```
WINNER: hypothesis 2 (src layout: src/ is not on sys.path when the tests run)
Team run: run-1 | check: `python -m unittest discover -s tests` (--check)
Copies started from: checkpoint episode_start (refs/breakpoint/checkpoints/<session>/1): the files when this problem started
Baseline (untouched copy): the check fails, as expected (0.25s)

Hypothesis 1: ... result: FAILED, time: copy 0.18s, session 10.7s, check 0.14s, total 11.27s, session: 3 turns, $0.17,
              session says (not trusted): ..., changes: ..., check output (last lines)
Hypothesis 2: ... PASSED ...
Winning change (hypothesis 2), as a diff against the starting point above:  ```diff ... ```
Apply these changes to the real project yourself, then rerun `<check>`. Nothing was changed in the real project.
Timing: prepare 0.29s | copy longest 0.38s, session longest 16.87s, check longest 0.15s | hypotheses (wall) 17.7s | total 18.14s
Full result: <project>/.claude/loop_state/tester_<session>.json
```

`NO_WINNER: <n> hypotheses tried` and `INCONCLUSIVE: <reason>` are the other first lines. The exit code is 0 whenever
the tester finished (any verdict). The full result (every hypothesis, diff, timings, session cost) is saved as JSON next
to the hook's notes (the folder is git-ignored).

---

## Safety

- **Copies only.** Every change happens in a worktree in the system temp folder. The real project's files, branch,
  index and history are never written (tests check this, including that `node_modules` survives).
- **Never auto-applied.** The tester prints the diff; the agent applies it in the real project where the user sees it,
  and the hook confirms it by watching the check pass (same as every other fix).
- **Two yeses.** The agent asks the user before running the tester, and Claude Code asks permission for the Bash call.
- **Specialist flag.** `BREAKPOINT_SPECIALIST=1` in every tester session: our hook does nothing there, so a tester
  can never start another team call or tester run. No MCP servers either.
- **Least permissions.** Edits only inside the copy, no commands except exact, listed package-manager commands, no web.
- **Shortcuts are caught.** A pass that came from skipping or silencing checks is `suspect`, never the winner.

---

## Timing (the team asked to measure this)

| Run | Hypotheses | Copy (longest) | Session (longest) | Check (longest) | Total |
| --- | --- | --- | --- | --- | --- |
| **Real `claude -p` on Sonnet**, tiny Python repo (src layout, `unittest`), 3 in parallel | 3 | 0.38 s | 16.9 s (10.7 / 13.7 / 16.9 s, 3–4 turns each) | 0.15 s | **18.1 s**, about $0.55 in total |
| Fake claude (`test_tester.py`), 3 in parallel | 3 | 0.5 s | 0.1 s | 0.2 s | 1.1–1.4 s |

So nearly all the time is the Claude sessions (startup alone is about 5–7 s, and each session pays about $0.17–0.19,
mostly for loading Claude Code's own system prompt). On a real project add: the check's own run time (twice: baseline
and per copy, in parallel), dependency copies when an install is needed (a large `node_modules` can take a minute or
more on Windows), and longer sessions for multi-file steps. A typical run should land around 0.5–3 minutes; the
`--timeout 180` cap bounds it at about `180 s + check time`. The agent runs it with a 10-minute Bash timeout.

---

## How to run it

```
# the command the hook gives (needs the server, or the mock: python plugin/hook/dev/mock_backend.py)
python plugin/tester/test_fixes.py --project "<project>" --session <session id> --run-id run-1 --check "npm test"

# with a report file instead of the server
python plugin/tester/test_fixes.py --project "<project>" --session <id> --report report.json --check "npm test"

# options: --max-hypotheses 3  --timeout 180  --check-timeout 120  --no-parallel  --deps link|copy|none
#          --link NAME  --setup "npm ci"  --model sonnet  --max-turns 30  --no-baseline  --keep

# with a fake claude (no cost): BREAKPOINT_TESTER_CLAUDE='["python", "plugin/hook/dev/fake_claude.py"]'
#                               BREAKPOINT_FAKE_PLAN=plan.json   (see the top of fake_claude.py)
```

Tests and demos:

```
python -m unittest discover -s plugin/hook/tests            # 126 tests, ~1 min; never calls the real claude
python plugin/hook/dev/mock_backend.py --selftest           # includes /api/help and /api/help/{run_id}
python plugin/hook/dev/replay.py plugin/hook/dev/scenarios/team_and_tester.jsonl   # the whole flow, fake claude
```

---

## Limitations

- **Shared global environment.** Copies share the machine: global Python packages, Node version, Docker, databases,
  running servers, environment variables. A fix that changes those cannot be proven in a copy (pip installs are never
  run), and a check that depends on a running service may behave differently. The baseline catches the worst case
  (check already passes).
- **Ignored files.** `.env` and other ignored files are not copied (they are not in a checkpoint and may hold secrets).
  A check that needs them will fail everywhere; use `--setup` to create a safe stand-in.
- **Dependency reuse.** Linked folders are shared by rule, not by force: the session is told not to touch them and has
  no commands that could, but an edit tool could in theory write into a linked folder. Hypotheses that install get a
  private copy, which can be slow for big `node_modules`. Nested dependency folders (monorepos) are not linked unless
  named with `--link`.
- **Starting point.** The diff is against `episode_start`. The real files may have changed since, so the agent applies
  the change by hand (the output says so) rather than with `git apply`.
- **One check for all.** Hypotheses with different checks are compared on one command.
- **Windows notes.** Junctions need no admin rights; `taskkill /T` kills timed-out sessions with their children;
  worktree removal is retried because a just-killed process can hold a file; output is UTF-8. The check runs in Git
  Bash (found next to `git.exe`, or `CLAUDE_CODE_GIT_BASH_PATH`), never WSL's `bash.exe`.
- **Cost.** Each hypothesis is one Sonnet session (about $0.15–0.20 on a tiny repo; more on a real one).
- **Background runs.** If the agent runs the tester in the background, the hook never sees its first line. The message
  tells it to run in the foreground; a WINNER that gets applied is still caught as a NOTABLE_SOLVE, just without the
  team credit.
