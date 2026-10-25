"""Brain: what the hook decides at each event (PROJECT_PLAN.md, docs/struggle-signals.md).

One handler per Claude Code event. Each takes (payload, state, backend) and returns the JSON to
print, or None to stay quiet. `state` is the notebook (notebook.py) and is changed in place; main.py
saves it. `backend` is the backend module, passed in so tests can use a fake one.

The help ladder. When struggles.py says the agent is struggling, the open episode climbs one rung
and the agent gets one message (at most once per NUDGE_COOLDOWN actions):
  1 FORUM   search the forum. A solved, close match -> KNOWN_FIX (we then watch the next run of that
            command and report the outcome). An open question -> OPEN_QUESTION_EXISTS. Nothing, or
            the server is down -> go straight to rung 2.
  (REWIND)  once per episode, before triage or the team: if the agent is going in circles (whack-a-mole,
            edit-run-same-output or the AI judge) and made 3+ edits since a checkpoint -> REWIND_OFFER
            (docs/rewind.md). The agent asks the user; rewind.py does the rewind on a yes.
  2 TRIAGE  is the error in the outside world (a library, tool, environment) or the project's own
            code? Own code -> STEP_BACK checklist once. External -> go straight to rung 3.
  3 TEAM    CALL_TEAM once: ask_for_help (server mode) or run_specialists (local mode).
  4 OQ      the team really ran (TEAM_STARTED) and gave up: no answer, or no fix passed -> POST_OPEN_QUESTION.

The team's replies (docs/local-tester.md). ask_for_help and check_help return text starting with a tag:
TEAM_STARTED <run_id> (team_ran, keep working), KNOWN_FIX / TRY_FIRST (no team run), TEAM_WORKING,
TEAM_REPORT (-> TEST_FIXES: the exact command for plugin/tester/test_fixes.py; the agent asks the user first),
TEAM_NO_ANSWER (-> POST_OPEN_QUESTION) and TEAM_FAILED (-> TEAM_STATUS: keep working). The tester's own output
starts with WINNER (noted as the episode's team_fix; NOTABLE_SOLVE credits the team once the check passes),
NO_WINNER (-> POST_OPEN_QUESTION) or INCONCLUSIVE (-> TEAM_STATUS: try the ranked fixes yourself).
A direct ask_for_help (not suggested by the hook) is tied to the command that is failing now, if any.

A command that kept failing and now passes closes the episode: NOTABLE_SOLVE, or SUSPECT_FIX when
the git diff shows the failure was hidden (|| true, skipped tests, ...) instead of fixed.
The backend is called only at these decision points, never on every event.

Checkpoints (checkpoints.py, git snapshots of the files): "last_good" when the goal command passes,
"episode_start" at the first failure of what may become a stuck episode. Both are skipped silently
when the project is not a git repo or git is slow.
"""

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import checkpoints
import messages
import notebook
import struggles

NUDGE_COOLDOWN = 3            # actions between two ladder messages
NOTABLE_MIN_FAILURES = 3      # failures before a pass counts as a notable solve
KNOWN_FIX_MIN_SCORE = 0.5     # a text match must score at least this to count
JUDGE_MIN_CONFIDENCE = 0.7
JUDGE_ACTIONS = 30            # actions sent to the AI judge
JUDGE_REPLIES = 5             # agent replies sent to the AI judge
TRIED_LIMIT = 8               # notes kept in episode["tried"]
TRANSCRIPT_TAIL_BYTES = 1_000_000
GIT_TIMEOUT = 2
REWIND_SIGNALS = {"whack_a_mole", "edit_run_same_output", "ai_judge"}   # going in circles, not one hard error
REWIND_MIN_EDITS = 3          # edits since a checkpoint before a rewind is worth offering
REPORT_TRIES = 3              # failures after TEAM_REPORT before the ladder asks for an Open Question

# The first line of an ask_for_help / check_help result (after the community label), and of the tester's output.
TEAM_TAG_RE = re.compile(r"^(TEAM_STARTED|TEAM_WORKING|TEAM_REPORT|TEAM_NO_ANSWER|TEAM_FAILED|KNOWN_FIX|TRY_FIRST)"
                         r"\b:?[ \t]*(.*)$", re.MULTILINE)
LABEL_RE = re.compile(r"^\s*\[Community-posted[^\]]*\]\s*")
TESTER_RESULT_RE = re.compile(r"^(WINNER|NO_WINNER|INCONCLUSIVE):[ \t]*(.*)$", re.MULTILINE)

# Test and build commands: their pass is a "last_good" point even before anything failed.
GOAL_COMMAND_RE = re.compile(r"\b(py\.?test|unittest|test|tests|jest|vitest|mocha|tsc|build|compile|cargo|mvn|"
                             r"gradle|tox)\b")

# The line that best names an error, for the short "already tried" notes.
KEY_LINE_RE = re.compile(r"\w*(Error|Exception)\b|^FAIL(ED)?\b|^E\s|\bassert\b|npm ERR!")

# Triage (rung 2). Anything that is not clearly the project's own code counts as external.
EXTERNAL_RE = re.compile(
    r"node_modules|site-packages|dist-packages|\.venv|\bpip\b|npm ERR|docker|ModuleNotFoundError|ImportError|"
    r"ERR_REQUIRE_ESM|version conflict|conflicting (peer )?dependenc|peer dep|ERESOLVE|command not found|"
    r"is not recognized as an internal or external command|permission denied|EACCES|connection refused|"
    r"ECONNREFUSED",
    re.IGNORECASE,
)
OWN_CODE_RE = re.compile(r"AssertionError|\bassert\b|expect\(|^\s*Expected|^\s*Received|^FAILED|\b[1-9]\d* failed\b",
                         re.MULTILINE)
FRAME_RE = re.compile(r'File "([^"]+)", line \d+|\bat (?:.*?\()?((?:[A-Za-z]:)?[^\s():]+):\d+:\d+')
LIBRARY_FRAME_RE = re.compile(r"lib[\\/]python|node:internal|<frozen|[\\/]Lib[\\/]", re.IGNORECASE)

# Fake-fix patterns in added lines of the git diff (PROJECT_PLAN.md, "Is a solution worth posting?").
FAKE_FIX_PATTERNS = [
    (re.compile(r"\|\|\s*true\b"), "`|| true` (ignores the failure)"),
    (re.compile(r"--no-verify"), "`--no-verify` (skips the checks)"),
    (re.compile(r"@ts-ignore"), "`@ts-ignore` (silences the type error)"),
    (re.compile(r"\.skip\s*\("), "`.skip(` (skips a test)"),
    (re.compile(r"\bxit\s*\("), "`xit(` (skips a test)"),
    (re.compile(r"@pytest\.mark\.skip"), "`@pytest.mark.skip` (skips a test)"),
    (re.compile(r"except[^:\n]*:\s*pass\b"), "`except: pass` (swallows the error)"),
]
BARE_EXCEPT_RE = re.compile(r"^\s*except\b[^:]*:\s*$")
TEST_DEF_RE = re.compile(r"^\s*(def (test_\w+)|(?:it|test|describe)\s*\(\s*['\"`]([^'\"`]*))")
TEST_FILE_RE = re.compile(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.[^/]+$|[^/]*\.(test|spec)\.[^/]+$)")


# ---------------------------------------------------------------------------
# Handlers, one per event
# ---------------------------------------------------------------------------

def on_session_start(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """Tell a new session about news for this project (an open question got solved, ...)."""
    if payload.get("source") == "compact":   # same session continuing; it already heard the news
        return None
    items = backend.get_updates(state["project_id"])
    if not items:
        return None
    return messages.context("SessionStart", messages.updates(items))


def on_user_prompt(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """Signal 6: the user pushes back ("still broken") or pastes an error we already saw."""
    prompt = str(payload.get("prompt") or "")
    notebook.record(state, {"kind": "user", "t": time.time()})
    signals = struggles.check_user_message(prompt, state["history"])
    if not struggles.is_struggling(signals):
        return None
    text = _nudge(payload, state, backend, signals, _problem(state, fallback=prompt))
    return messages.context("UserPromptSubmit", text) if text else None


def on_tool_use(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """PostToolUse and PostToolUseFailure: record the action, then decide after commands."""
    event = payload.get("hook_event_name", "PostToolUse")
    if str(payload.get("tool_name", "")).startswith("mcp__"):
        text = _after_mcp_tool(payload, state)
    else:
        action, output = notebook.action_from_tool_use(payload)
        if action is not None and action["kind"] == "run" and _is_tester_command(action["cmd"]):
            text = _after_tester(state, output)   # our own tester: not work on the problem, never a struggle
            return messages.context(event, text) if text else None
        if action is None or (action["kind"] == "run" and _is_rewind_command(action["cmd"])):
            return None   # tools we don't track, and our own rewind.py (not work on the problem)
        notebook.record(state, action)
        text = _after_run(payload, state, backend, action, output) if action["kind"] == "run" else None
    return messages.context(event, text) if text else None


def on_stop(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """Signals 4 and 5, and the open question reminder. Blocks at most once per episode."""
    if payload.get("stop_hook_active"):
        return None
    episode = state.get("episode")
    if episode and episode.get("stop_blocked"):
        return None
    history = state["history"]
    replies = assistant_replies(payload.get("transcript_path"), 1)
    signals = {s.name: s for s in struggles.check_stop(history, replies[-1] if replies else "")}

    key, text = "", None
    if "says_done_isnt" in signals:
        signal = signals["says_done_isnt"]
        key = signal.key
        run = _last_run(history, key) or {}
        text = messages.not_done(run.get("cmd", key), run.get("err_fp"), signal.reason)
    elif "unchecked_success_claim" in signals:
        failed = _last_failed_run(history[_turn_start(history):]) or {}
        key = failed.get("key", "")
        text = messages.unverified_claim(failed.get("cmd", ""), signals["unchecked_success_claim"].reason)
    elif episode and episode.get("key") and _open_question_due(state, episode, at_stop=True):
        command, _, fp = _problem(state)
        episode["rung"] = 4
        text = messages.post_open_question(command, fp, episode["tried"],
                                           "The team was called, but the problem was never confirmed fixed.")
    elif episode and episode.get("key") and episode.get("team_status") == "working" \
            and not (_last_run(history, episode["key"]) or {}).get("ok"):
        command, _, fp = _problem(state)
        text = messages.team_status(
            f"The team is still researching (run {episode.get('team_run_id')}) and `{command}` still fails.",
            f"call `check_help` with run_id {episode.get('team_run_id')} before you finish, or tell the user the "
            "team is still working.", command, fp)
    if not text:
        return None
    episode = episode or _open_episode(state, key)
    episode["stop_blocked"] = True
    return messages.block(text)


# ---------------------------------------------------------------------------
# After a command
# ---------------------------------------------------------------------------

def _after_run(payload: dict[str, Any], state: dict[str, Any], backend: Any,
               run: dict[str, Any], output: str) -> str | None:
    history = state["history"]
    idx = len(history) - 1
    key = run["key"]
    if struggles._is_probe(key):
        return None
    episode = state.get("episode")
    if run["ok"] and _is_goal_pass(state, run, idx):
        _save_checkpoint(payload, state, "last_good", run, idx)
    elif not run["ok"] and episode is None and _starts_failing(history, key, idx):
        _save_checkpoint(payload, state, "episode_start", run, idx)
    if not run["ok"]:
        state["last_error"] = {"cmd": run["cmd"], "key": key, "fp": run["err_fp"],
                               "text": output[-notebook.ERROR_CHARS:]}
        if episode and not episode["key"]:   # opened by a direct ask_for_help: the next failure is what it's about
            episode.update(key=key, started_idx=idx, fp=run["err_fp"])
            state["goal_key"] = key
        if episode and key == episode["key"]:
            episode["fp"] = run["err_fp"] or episode["fp"]
            _note_attempt(history, episode, idx, output)

    # (a) A forum fix was suggested for this command: this run is its test.
    if episode and episode.get("pending_fix") and key == episode["pending_fix"]["key"]:
        return _check_fix(payload, state, backend, run, output)

    # (b) The command passes after failing.
    if run["ok"]:
        same_key = bool(episode) and episode["key"] == key
        failures = _failures_before(history, key, idx, episode["started_idx"] if same_key else 0)
        team_fix = episode.get("team_fix") if same_key else None
        if failures >= NOTABLE_MIN_FAILURES or (team_fix and failures):
            fp = (_last_failed_run(history[:idx], key) or {}).get("err_fp")
            tried = list(episode["tried"]) if same_key else []
            if same_key:
                state["episode"] = None
            findings = suspect_changes(notebook.project_dir(payload))
            if findings:
                return messages.suspect_fix(run["cmd"], failures, fp, findings)
            return messages.notable_solve(run["cmd"], failures, fp, tried, team_fix)
        if same_key and failures:
            state["episode"] = None   # fixed quickly: nothing worth saying
            return None

    # (c) Is the agent struggling?
    signals = struggles.check_after_run(history)
    if not any(s.strength == "strong" for s in signals):
        signals += _ask_judge(payload, state, backend)
    if not struggles.is_struggling(signals):
        return None
    about_this_run = not run["ok"] or any(s.key == key for s in signals)
    problem = (run["cmd"], output, run["err_fp"]) if about_this_run else _problem(state)
    return _nudge(payload, state, backend, signals, problem)


def _check_fix(payload: dict[str, Any], state: dict[str, Any], backend: Any,
               run: dict[str, Any], output: str) -> str | None:
    """Report whether the suggested forum fix worked, judged only by the command's result."""
    episode = state["episode"]
    fix = episode["pending_fix"]
    episode["pending_fix"] = None
    # Report against the error the post was matched on, not whatever this run printed.
    backend.report_outcome(fix["post_id"], run["ok"], fix.get("fp") or episode["fp"], run["cmd"],
                           str(payload.get("session_id", "")))
    if run["ok"]:
        state["episode"] = None
        return None
    _add_tried(episode, f"forum fix from post #{fix['post_id']} -> did not work")
    text = messages.fix_failed(fix["post_id"], run["cmd"], run["err_fp"], episode["tried"])
    episode["rung"] = max(episode["rung"], 1)   # forum done; go on to triage now
    problem = (run["cmd"], output, run["err_fp"])
    reason = f"The forum fix from post #{fix['post_id']} did not work."
    next_step = _climb(state, backend, problem, reason)
    episode["last_nudge_idx"] = len(state["history"])
    return text + "\n\n" + next_step if next_step else text


def _ask_judge(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> list[struggles.Signal]:
    """The AI judge, for the gray zone where no rule fired (at most once per JUDGE_EVERY actions)."""
    history = state["history"]
    if not struggles.judge_due(history, time.time(), state.get("last_judge")):
        return []
    state["last_judge"] = len(history)
    verdict = backend.judge(str(payload.get("session_id", "")), recent_actions(history),
                            assistant_replies(payload.get("transcript_path"), JUDGE_REPLIES))
    try:
        stuck = verdict is not None and verdict.get("stuck") is True \
            and float(verdict.get("confidence", 0)) >= JUDGE_MIN_CONFIDENCE
    except (TypeError, ValueError):
        stuck = False
    if not stuck:
        return []
    return [struggles.Signal("ai_judge", "strong", f"AI judge: {verdict.get('reason') or 'the agent looks stuck.'}")]


def _after_mcp_tool(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Track our own MCP tools: the team was called, an open question was posted, a fix was shared."""
    name = str(payload.get("tool_name", "")).lower()
    episode = state.get("episode")
    if "ask_for_help" in name or "check_help" in name:
        return _after_team_tool(payload, state, "ask_for_help" in name)
    if "run_specialists" in name:
        if not episode and _last_failed_run(state["history"]):
            episode = _open_episode(state, "")
        if not episode:
            return None
        episode["team_ran"] = True
        episode["rung"] = max(episode["rung"], 3)
        result = notebook.tool_response_text(payload).lstrip()   # "WINNER: ..." or "NO_WINNER: ..."
        if "run_specialists" in name and result.startswith("WINNER"):
            state["episode"] = None   # the tool already confirmed the fix in this project
            return None
        if "run_specialists" in name and result.startswith("NO_WINNER") and not episode["oq_posted"]:
            episode["rung"] = 4
            episode["team_status"] = "no_winner"
            command, _, fp = _problem(state)
            episode["last_nudge_idx"] = len(state["history"])
            return messages.post_open_question(command, fp, episode["tried"],
                                               "The specialist team found no verified fix.")
    elif "post_open_question" in name and episode:
        episode["oq_posted"] = True
    elif name.endswith("__post") and episode:
        state["episode"] = None   # solved and shared
    return None


def _team_reply(text: str) -> tuple[str | None, str]:
    """(tag, rest of its line) of a team tool's result, after the community label; (None, "") if untagged."""
    text = LABEL_RE.sub("", text.replace(messages.COMMUNITY_LABEL, ""), count=1).lstrip()
    match = TEAM_TAG_RE.match(text)
    return (match.group(1), match.group(2).strip()) if match else (None, "")


def _failing_key(history: list[dict[str, Any]]) -> str:
    """The most recent command that failed and has not passed since, or ""."""
    last = _last_failed_run(history)
    if not last:
        return ""
    latest = _last_run(history, last.get("key"))
    return last.get("key", "") if latest is not None and not latest.get("ok") else ""


def _after_team_tool(payload: dict[str, Any], state: dict[str, Any], asked: bool) -> str | None:
    """ask_for_help / check_help results. Only TEAM_STARTED (or a later team answer) means the team really ran."""
    tag, rest = _team_reply(notebook.tool_response_text(payload))
    episode = state.get("episode")
    if not tag:
        if asked and episode:
            episode["rung"] = max(episode["rung"], 3)   # it escalated; the reply was an error or unknown
        return None
    if episode is None:   # a direct call the hook did not suggest: tie it to the failing command, if any
        key = _failing_key(state["history"])
        episode = _open_episode(state, key) if key else notebook.new_episode("", None, len(state["history"]))
        state["episode"] = episode
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    run_id = str(tool_input.get("run_id") or "") or (rest.split() or [""])[0].strip(".,:;") \
        or str(episode.get("team_run_id") or "")
    command, _, fp = _problem(state) if episode["key"] else ("", "", None)
    tried = episode["tried"]

    if tag in ("KNOWN_FIX", "TRY_FIRST"):   # answered without a team run
        episode["rung"] = max(episode["rung"], 1 if tag == "KNOWN_FIX" else 2)
        post = re.search(r"#(\d+)", rest)
        if tag == "KNOWN_FIX" and post and episode["key"] and not episode.get("pending_fix"):
            episode["pending_fix"] = {"post_id": int(post.group(1)), "key": episode["key"], "fp": episode["fp"]}
        return None
    episode["rung"] = max(episode["rung"], 3)
    if tag == "TEAM_FAILED":
        episode.update(team_ran=False, team_status="failed")
        return messages.team_status("The team run failed (a server problem, not an answer). This is not an Open "
                                    "Question.", "keep working on the problem yourself; the hook suggests the next "
                                    "step if you stay stuck.", command, fp, tried)
    episode["team_ran"] = True
    if run_id:
        episode["team_run_id"] = run_id
    if tag in ("TEAM_STARTED", "TEAM_WORKING"):
        if episode.get("team_status") in (None, "working", "failed"):
            episode["team_status"] = "working"
        return None
    if tag == "TEAM_NO_ANSWER":
        episode["team_status"] = "no_answer"
        if episode["oq_posted"]:
            return None
        episode["rung"] = 4
        episode["last_nudge_idx"] = len(state["history"])
        return messages.post_open_question(command, fp, tried, "The team researched it and found no likely cause.")
    # TEAM_REPORT
    if episode.get("team_status") not in ("winner", "no_winner"):
        episode["team_status"] = "report"
    episode["team_report_idx"] = len(state["history"])
    if not run_id or episode.get("tester_offered") == run_id:
        return None   # check_help again for the same report: already offered
    episode["tester_offered"] = run_id
    episode["last_nudge_idx"] = len(state["history"])
    check = (_last_run(state["history"], episode["key"]) or {}).get("cmd", command) if episode["key"] else ""
    tester = messages.tester_command(notebook.project_dir(payload), str(payload.get("session_id") or ""), run_id,
                                     check)
    return messages.test_fixes(tester, run_id, command, fp, tried)


def _after_tester(state: dict[str, Any], output: str) -> str | None:
    """The agent ran plugin/tester/test_fixes.py: read its first line (WINNER / NO_WINNER / INCONCLUSIVE)."""
    match = TESTER_RESULT_RE.search(output or "")
    episode = state.get("episode")
    if not match or not episode:
        return None
    verdict, rest = match.group(1), match.group(2).strip()
    command, _, fp = _problem(state) if episode["key"] else ("", "", None)
    if verdict == "WINNER":
        found = re.match(r"hypothesis (\d+)\s*(?:\((.*)\))?\s*$", rest)
        number, cause = (int(found.group(1)), found.group(2) or "") if found else (None, rest)
        episode.update(team_status="winner", team_fix={"hypothesis": number, "cause": cause,
                                                       "run_id": episode.get("team_run_id")})
        _add_tried(episode, f"local tester: team hypothesis {number} ({cause}) passed in a copy")
        return None   # the agent applies it; the next pass of the command closes the episode
    if verdict == "NO_WINNER":
        episode.update(team_ran=True, team_status="no_winner")
        _add_tried(episode, f"local tester: {rest or 'the hypotheses'} in copies, none passed")
        if episode["oq_posted"]:
            return None
        episode["rung"] = 4
        episode["last_nudge_idx"] = len(state["history"])
        return messages.post_open_question(command, fp, episode["tried"],
                                           "The local tester tried the team's fixes in copies of the project and "
                                           "none made the check pass.")
    return messages.team_status(f"The tester could not prove a fix: {rest}",
                                f"try the team's ranked fixes yourself, in rank order, rerunning "
                                f"`{command or 'the check'}` after each.", command, fp, episode["tried"])


def _open_question_due(state: dict[str, Any], episode: dict[str, Any], at_stop: bool = False) -> bool:
    """Only when the team really ran and gave up: no answer, no fix passed in the tester, or (after a report) the
    command kept failing (still failing at Stop, REPORT_TRIES failures on the ladder). Never without a command."""
    if not episode.get("key") or not episode.get("team_ran") or episode.get("oq_posted"):
        return False
    status = episode.get("team_status")
    if status in ("no_answer", "no_winner"):
        return True
    if status not in ("report", None):   # working, winner, failed
        return False
    since = (episode.get("team_report_idx") or 0) if status == "report" and not at_stop else 0
    runs = [a for a in state["history"][since:] if _is_run(a, episode["key"])]
    if status is None or at_stop:   # status None: run_specialists with an unrecognized result (the old rule)
        return bool(runs) and not runs[-1].get("ok")
    return sum(1 for a in runs if not a.get("ok")) >= REPORT_TRIES


# ---------------------------------------------------------------------------
# The help ladder
# ---------------------------------------------------------------------------

def _nudge(payload: dict[str, Any], state: dict[str, Any], backend: Any,
           signals: list[struggles.Signal], problem: tuple[str, str, str | None]) -> str | None:
    """Struggling: open an episode if needed and climb one rung, unless we nudged very recently."""
    episode = state.get("episode")
    if episode is None:
        episode = _open_episode(state, next((s.key for s in signals if s.key), ""))
    elif not episode["key"]:
        episode["key"] = struggles.command_key(problem[0])
    length = len(state["history"])
    if episode["last_nudge_idx"] is not None and length - episode["last_nudge_idx"] < NUDGE_COOLDOWN:
        return None
    reason = " ".join(s.reason for s in signals if s.strength == "strong")
    text = _climb(state, backend, problem, reason, payload, signals)
    if text:
        episode["last_nudge_idx"] = length
    return text


def _climb(state: dict[str, Any], backend: Any, problem: tuple[str, str, str | None], reason: str,
           payload: dict[str, Any] | None = None, signals: list[struggles.Signal] | None = None) -> str | None:
    episode = state["episode"]
    command, error, fp = problem
    tried = episode["tried"]

    if episode["rung"] < 1:
        episode["rung"] = 1
        results = backend.search(error, command, fp)
        top = results[0] if results else None
        if top and _close_match(top):
            if top.get("status") == "solved" and top.get("solution"):
                episode["pending_fix"] = {"post_id": top.get("id"), "key": struggles.command_key(command), "fp": fp}
                return messages.known_fix(top, command, fp, tried)
            if top.get("status") == "open":
                episode["open_question_id"] = top.get("id")
                return messages.open_question_exists(top, command, fp, tried)
        # no match, or the server is down: go on to triage now

    if episode["rung"] < 3 and payload is not None:   # the rewind sits between the forum and the team
        text = _rewind_offer(payload, state, signals or [], problem, reason)
        if text:
            return text

    if episode["rung"] < 2:
        episode["rung"] = 2
        kind, why = classify(error)
        if kind == "own_code":
            return messages.step_back(command, fp, tried, why)

    if episode["rung"] < 3:
        episode["rung"] = 3
        return messages.call_team(backend.team_mode(), command, error, fp, tried, reason,
                                  episode.get("open_question_id"))

    if episode["rung"] < 4 and _open_question_due(state, episode):
        episode["rung"] = 4
        return messages.post_open_question(command, fp, tried, reason)
    return None


def _rewind_offer(payload: dict[str, Any], state: dict[str, Any], signals: list[struggles.Signal],
                  problem: tuple[str, str, str | None], reason: str) -> str | None:
    """REWIND_OFFER, once per episode, when the agent goes in circles with 3+ edits since a checkpoint."""
    episode = state["episode"]
    if episode.get("rewind_offered") or not REWIND_SIGNALS & {s.name for s in signals}:
        return None
    candidates: list[tuple[str, dict[str, Any], int]] = []
    for name in ("last_good", "episode_start"):
        entry = notebook.checkpoint_for(state, name)
        if not entry or any(entry["ref"] == other["ref"] for _, other, _ in candidates):
            continue   # none saved, or the same files as the option before it
        edits, _ = notebook.edits_since(state, entry)
        if edits >= REWIND_MIN_EDITS:
            candidates.append((name, entry, edits))
    if not candidates:
        return None
    project = notebook.project_dir(payload)
    diff = checkpoints.summary(project, [entry["ref"] for _, entry, _ in candidates], GIT_TIMEOUT)
    if not diff.get("ok"):
        return None   # git can't tell what a rewind would undo (ref gone, git slow): don't offer
    session = str(payload.get("session_id") or "")
    options = []
    for name, entry, edits in candidates:
        changes = diff["summaries"][entry["ref"]]
        if not changes["files"]:
            continue   # the files already match that point: nothing to undo
        cmd = entry.get("cmd") or entry.get("key") or ""
        title = f"the last time `{cmd}` passed" if name == "last_good" else "when this problem started"
        options.append({"name": name, "title": title, "cmd": cmd, "time": messages.clock(entry["time"]),
                        "edits": edits, **changes, "command": messages.rewind_command(project, session, name)})
    if not options:
        return None
    episode["rewind_offered"] = True
    command, _, fp = problem
    return messages.rewind_offer(options, command, fp, episode["tried"], reason)


def _close_match(post: dict[str, Any]) -> bool:
    try:
        score = float(post.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return post.get("match") == "fingerprint" or score >= KNOWN_FIX_MIN_SCORE


def classify(error: str) -> tuple[str, str]:
    """("external" | "own_code", why). Unknown counts as external: the team can still research it."""
    external = EXTERNAL_RE.search(error or "")
    if external:
        return "external", f"the error mentions `{external.group(0)}`."
    frames = [a or b for a, b in FRAME_RE.findall(error or "")]
    if frames and not any(LIBRARY_FRAME_RE.search(f) for f in frames):
        return "own_code", "the stack trace points only at files in this project."
    own = OWN_CODE_RE.search(error or "")
    if own:
        return "own_code", f"the failure is a test assertion (`{own.group(0).strip()}`)."
    return "external", "nothing points at the project's own code."


# ---------------------------------------------------------------------------
# Episodes and what was tried
# ---------------------------------------------------------------------------

def _open_episode(state: dict[str, Any], key: str) -> dict[str, Any]:
    """Start an episode about `key` (or the last failing command), going back to its first failure."""
    history = state["history"]
    if not key:
        key = (_last_failed_run(history) or {}).get("key", "")
    idx = len(history) - 1
    start = _streak_start(history, key, idx) if key else max(idx, 0)
    last = _last_failed_run(history[start:], key) if key else None
    episode = notebook.new_episode(key, (last or {}).get("err_fp"), start)
    if key:
        state["goal_key"] = key   # its next pass marks a "last_good" checkpoint, even after the episode closes
    for i in range(start, len(history)):
        action = history[i]
        if action.get("kind") == "run" and action.get("key") == key and not action.get("ok"):
            _note_attempt(history, episode, i, action.get("tail", ""))
    state["episode"] = episode
    return episode


def _note_attempt(history: list[dict[str, Any]], episode: dict[str, Any], idx: int, output: str) -> None:
    """A short note for a failed run: what was edited before it, and the key error line."""
    key = history[idx].get("key")
    edits: list[str] = []
    first = True
    for action in reversed(history[episode["started_idx"]:idx]):
        if action.get("kind") == "run" and action.get("key") == key:
            first = False
            break
        if action.get("kind") == "edit":
            name = Path(action.get("path", "")).name
            if name and name not in edits:
                edits.insert(0, name)
    if edits:
        did = f"edited {', '.join(edits)} -> still failed"
    else:
        did = "first run failed" if first else "reran -> failed"
    _add_tried(episode, f"{did}: {_key_error_line(output)}")


def _add_tried(episode: dict[str, Any], note: str) -> None:
    if episode["tried"][-1:] != [note]:
        episode["tried"].append(note)
    del episode["tried"][:-TRIED_LIMIT]


def _key_error_line(output: str) -> str:
    """The most telling line: an `XxxError:` / `FAILED` / assertion line if any, else the last error-looking one."""
    lines = [ln.strip(" =-") for ln in (output or "").splitlines()]
    lines = [ln for ln in lines if ln and not re.match(r"(?i)exit code -?\d+$", ln)]
    best = [ln for ln in lines if KEY_LINE_RE.search(ln)]
    errors = [ln for ln in lines if struggles.ERROR_LINE_RE.search(ln)]
    line = (best or errors or lines or ["no output"])[-1]
    return line[:120]


def _problem(state: dict[str, Any], fallback: str = "") -> tuple[str, str, str | None]:
    """(command, error text, fingerprint) of the most recent failure, for searches and messages."""
    last = state.get("last_error")
    if last:
        return last.get("cmd", ""), last.get("text", ""), last.get("fp")
    return "", fallback, struggles.fingerprint(fallback)


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def _save_checkpoint(payload: dict[str, Any], state: dict[str, Any], label: str,
                     run: dict[str, Any], idx: int) -> None:
    """Snapshot the files (checkpoints.py) and note it. Silent on any failure: rewind is a bonus."""
    try:
        saved = checkpoints.save(notebook.project_dir(payload), str(payload.get("session_id") or ""), label,
                                 GIT_TIMEOUT)
        if saved.get("ok"):
            notebook.add_checkpoint(state, saved, label, idx, run)
    except Exception:
        pass


def _is_goal_pass(state: dict[str, Any], run: dict[str, Any], idx: int) -> bool:
    """Is this pass a "last_good" point? During an episode: only its own command. Otherwise: the last
    episode's command, any command that failed last time, or a test or build command."""
    episode = state.get("episode")
    key = run["key"]
    if episode and episode.get("key"):
        return key == episode["key"]
    previous = _last_run(state["history"][:idx], key)
    return key == state.get("goal_key") or bool(previous and not previous.get("ok")) \
        or bool(GOAL_COMMAND_RE.search(key))


def _starts_failing(history: list[dict[str, Any]], key: str, idx: int) -> bool:
    """The first failure of `key` since it last passed: where a stuck episode would start."""
    previous = _last_run(history[:idx], key)
    return previous is None or bool(previous.get("ok"))


def _is_tester_command(cmd: str) -> bool:
    """The agent running our local tester (plugin/tester/test_fixes.py)."""
    return messages.TESTER_SCRIPT.name in cmd and "--session" in cmd


def _is_rewind_command(cmd: str) -> bool:
    """The agent running our own rewind.py: not part of its work on the problem."""
    return messages.REWIND_SCRIPT.name in cmd and "--session" in cmd


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def _is_run(action: dict[str, Any], key: str | None = None) -> bool:
    return action.get("kind") == "run" and not struggles._is_probe(action.get("key", "")) \
        and (key is None or action.get("key") == key)


def _last_run(history: list[dict[str, Any]], key: str | None = None) -> dict[str, Any] | None:
    return next((a for a in reversed(history) if _is_run(a, key)), None)


def _last_failed_run(history: list[dict[str, Any]], key: str | None = None) -> dict[str, Any] | None:
    return next((a for a in reversed(history) if _is_run(a, key) and not a.get("ok")), None)


def _failures_before(history: list[dict[str, Any]], key: str, idx: int, start: int = 0) -> int:
    """Failures of `key` before index `idx`, back to its previous pass (and not before `start`)."""
    count = 0
    for i in range(idx - 1, max(start, 0) - 1, -1):
        if _is_run(history[i], key):
            if history[i].get("ok"):
                break
            count += 1
    return count


def _streak_start(history: list[dict[str, Any]], key: str, idx: int) -> int:
    """Index of the first failure in `key`'s current run of failures (idx if there is none)."""
    start = idx
    for i in range(idx, -1, -1):
        if _is_run(history[i], key):
            if history[i].get("ok") and i != idx:
                break
            if not history[i].get("ok"):
                start = i
    return max(start, 0)


def _turn_start(history: list[dict[str, Any]]) -> int:
    return next((i + 1 for i in range(len(history) - 1, -1, -1) if history[i].get("kind") == "user"), 0)


def recent_actions(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last JUDGE_ACTIONS actions for POST /v1/judge: {tool, summary, ok, out}, plus the raw
    history fields (kind, cmd/path, err_fp) that docs/hook-contracts.md shows."""
    tools = {"run": "Bash", "edit": "Edit", "read": "Read", "user": "user_message"}
    actions = []
    for a in history[-JUDGE_ACTIONS:]:
        raw = {k: a[k] for k in ("kind", "cmd", "path", "err_fp") if a.get(k) is not None}
        actions.append({**raw, "tool": tools.get(a.get("kind", ""), str(a.get("kind"))),
                        "summary": str(a.get("cmd") or a.get("path") or ""),
                        "ok": bool(a.get("ok", True)), "out": str(a.get("tail", ""))})
    return actions


# ---------------------------------------------------------------------------
# Reading the outside world: the transcript and the git diff
# ---------------------------------------------------------------------------

def assistant_replies(transcript_path: Any, limit: int) -> list[str]:
    """The agent's last `limit` text replies from the conversation log (JSONL). [] if unreadable."""
    if not transcript_path:
        return []
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TRANSCRIPT_TAIL_BYTES))
            data = f.read()
    except OSError:
        return []
    replies = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue   # includes the partial first line after seeking
        if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        else:
            texts = []
        text = "\n".join(t for t in texts if isinstance(t, str) and t).strip()
        if text:
            replies.append(text)
    return replies[-limit:]


def _git_diff(folder: Path) -> str:
    for args in (["git", "diff", "HEAD"], ["git", "diff"]):   # `git diff` alone if there is no commit yet
        try:
            result = subprocess.run(args, cwd=folder, capture_output=True, timeout=GIT_TIMEOUT,
                                    creationflags=notebook.NO_WINDOW)
        except Exception:
            return ""
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
    return ""


def suspect_changes(folder: Path) -> list[str]:
    """Signs in the uncommitted diff that a pass was faked: silenced errors, skipped or deleted tests."""
    lines = _git_diff(folder).splitlines()
    findings: list[str] = []
    added: set[str] = set()
    removed: list[tuple[str, str, str]] = []
    path, deleted = "", False
    for i, line in enumerate(lines):
        if line.startswith("diff --git"):
            match = re.match(r"diff --git a/.+? b/(.+)$", line)
            path, deleted = (match.group(1) if match else ""), False
        elif line.startswith("deleted file mode"):
            deleted = True
            if TEST_FILE_RE.search(path):
                findings.append(f"deleted test file {path}")
        elif line.startswith(("+++", "---")):
            continue
        elif line.startswith("+"):
            text = line[1:]
            added.add(text.strip())
            for pattern, what in FAKE_FIX_PATTERNS:
                if pattern.search(text):
                    findings.append(f"added {what} in {path}")
            if BARE_EXCEPT_RE.match(text) and i + 1 < len(lines) and re.match(r"^\+\s*pass\s*$", lines[i + 1]):
                findings.append(f"added `except: pass` (swallows the error) in {path}")
        elif line.startswith("-") and not deleted:
            match = TEST_DEF_RE.match(line[1:])
            if match:
                removed.append((path, line[1:].strip(), match.group(2) or match.group(3) or "test"))
    findings += [f"removed test `{name}` from {path}" for path, text, name in removed if text not in added]
    return list(dict.fromkeys(findings))[:10]
