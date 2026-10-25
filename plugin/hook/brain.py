"""Brain: what the hook decides at each event (PROJECT_PLAN.md, docs/struggle-signals.md).

One handler per Claude Code event. Each takes (payload, state, backend) and returns the JSON to
print, or None to stay quiet. `state` is the notebook (notebook.py) and is changed in place; main.py
saves it. `backend` is the backend module, passed in so tests can use a fake one.

The early forum check. A strong model often fixes a bug in 1-2 tries, so it may never count as stuck. So the
first time a command fails with an error fingerprint this session has not looked up yet (state["looked_up"]),
the hook searches the forum once, right away. A close solved match -> KNOWN_FIX, a close open question ->
OPEN_QUESTION_EXISTS: both open the episode (flagged "early") and count as rung 1 done. Nothing, or the server is
down -> silent, and everything below works as if the check never ran. While an episode is only "early" (the agent
does not count as stuck yet), a failed KNOWN_FIX gets FIX_FAILED alone: the ladder goes on once it is stuck.

The help ladder. When struggles.py says the agent is struggling, the open episode climbs one rung
and the agent gets one message (_cooled_down: after a message, wait NUDGE_COOLDOWN actions or
NUDGE_COOLDOWN_FAILURES failed runs, whichever comes first; one failure is enough after a REWIND_OFFER, or when it
shows a new error):
  1 FORUM   search the forum. A solved, close match -> KNOWN_FIX (we then watch the next run of that
            command and report the outcome). An open question -> OPEN_QUESTION_EXISTS. Nothing, or
            the server is down -> go straight to rung 2.
  (REWIND)  once per episode, before triage or the team: if the agent is going in circles (whack-a-mole,
            edit-run-same-output or the AI judge) and made 3+ edits since a checkpoint -> REWIND_OFFER
            (docs/rewind.md). The agent asks the user; rewind.py does the rewind on a yes.
  2 TRIAGE  is the error in the outside world (a library, tool, environment) or the project's own
            code? Own code -> STEP_BACK checklist once. External -> go straight to rung 3.
  3 TEAM    once, by the team_mode setting (backend.team_mode):
              ask (default) the stuck menu below, instead of REWIND_OFFER now and the team later.
              auto          the hook starts the team itself (_start_team) and says so (TEAM_CALLED).
              off           nothing: the team is never started or suggested (REWIND_OFFER stays as before).
  4 OQ      the team really ran (TEAM_STARTED) and gave up: no answer, or no fix passed -> POST_OPEN_QUESTION.

The stuck menu (team_mode ask). Where the ladder would send REWIND_OFFER or the team step, it sends STUCK_MENU once
per episode instead: call AskUserQuestion now with "Call the specialist team", "Rewind the files to before the problem
started" (only when _rewind_options finds a rewind) and "Let the agent keep trying". state["pending_menu"] holds it
until it is answered:
  - PreToolUse (on_pre_tool_use): any other tool call while it waits ends the session ({"continue": false}) with the
    menu as text for the user ("Type 1, 2 or 3").
  - The answer: the AskUserQuestion result (PostToolUse), or the user typing 1 / 2 / 3 or the option's words
    (UserPromptSubmit). Any other prompt drops the menu.
  - Team: the hook sends POST /api/help itself (case_builder.py, the same case file as `ask_for_help`), marks the
    episode as team_ran exactly like a TEAM_STARTED reply, and says TEAM_CALLED (call check_help with the run id).
    If the request fails: CALL_TEAM, so the agent calls ask_for_help itself. Rewind: REWIND_OFFER, with the user's
    yes already given. Keep trying: nothing more this episode until the next rung (the open question) is due.

The team's replies (docs/local-tester.md). ask_for_help and check_help return text starting with a tag:
TEAM_STARTED <run_id> (team_ran, keep working), KNOWN_FIX / TRY_FIRST (no team run), TEAM_WORKING,
TEAM_REPORT (-> TEST_FIXES: the exact command for plugin/tester/test_fixes.py; the agent asks the user first),
TEAM_NO_ANSWER (-> POST_OPEN_QUESTION) and TEAM_FAILED (-> TEAM_STATUS: keep working). The tester's own output
starts with WINNER (noted as the episode's team_fix; NOTABLE_SOLVE credits the team once the check passes),
NO_WINNER (-> POST_OPEN_QUESTION) or INCONCLUSIVE (-> TEAM_STATUS: try the ranked fixes yourself).
A direct ask_for_help (not suggested by the hook) is tied to the command that is failing now, if any.

A command that kept failing and now passes closes the episode: NOTABLE_SOLVE (with the open question's id when
the forum has one, so the post answers it), or SUSPECT_FIX when the git diff shows the failure was hidden
(|| true, skipped tests, ...) instead of fixed. "Kept failing" means NOTABLE_MIN_FAILURES failures, or any failure
at all when the episode is about a known open question or a fix the tester proved (those are always worth a post).

Only our own MCP tools count (OUR_TOOL_PREFIXES), and `post` / `post_open_question` only when their reply says the
post was saved: an error or a REVISE reply leaves the episode as it was.

LEAVE_REPLY: only for a solution that was ALREADY POSTED on the forum, never for the team's or the tester's fixes
(those get NOTABLE_SOLVE: a new post). It is sent when the command that was failing passes after either
  - a KNOWN_FIX handoff (the hook's own search, or ask_for_help answering KNOWN_FIX), or
  - the agent's own `search` / `get_post` call that showed a SOLVED post while that command was failing
    (state["viewed_posts"]; the most recent one counts, and a KNOWN_FIX post wins over it).
The outcome is reported for that post first. At most once per post per session (state["reply_nudged"], which also
holds posts the agent already replied to), and never at Stop.
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

import backend as settings   # only for settings (team_mode); calls go through the `backend` passed in, so tests fake it
import case_builder
import checkpoints
import messages
import notebook
import struggles

NUDGE_COOLDOWN = 3            # actions between two ladder messages ...
NUDGE_COOLDOWN_FAILURES = 2   # ... or failed runs, whichever comes first (one combined fix-and-test call = 1 action)
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
TEAM_FILES = 5                # recently edited files sent in the hook's own case file

# The stuck menu. While it waits, only these tools may run: the question itself, and loading it or the skill.
ASK_TOOL = "AskUserQuestion"
MENU_ALLOWED_TOOLS = {ASK_TOOL, "ToolSearch", "Skill"}
MENU_KEYWORDS = {"team": ("team", "specialist"), "rewind": ("rewind", "roll back", "rollback"),
                 "keep": ("keep", "trying", "let the agent", "continue")}
MENU_NUMBER_RE = re.compile(r"^\s*(?:option\s*)?#?\s*([1-9])\s*[.)]?\s*$", re.IGNORECASE)
MENU_TYPED_CHARS = 60         # a longer prompt is a new request, not a typed menu answer
ANSWER_PAIR_RE = re.compile(r'"[^"\n]*"\s*=\s*"([^"\n]*)"')   # AskUserQuestion's text result: "question"="answer"

# The first line of an ask_for_help / check_help result (after the community label), and of the tester's output.
TEAM_TAG_RE = re.compile(r"^(TEAM_STARTED|TEAM_WORKING|TEAM_REPORT|TEAM_NO_ANSWER|TEAM_FAILED|KNOWN_FIX|TRY_FIRST)"
                         r"\b:?[ \t]*(.*)$", re.MULTILINE)
LABEL_RE = re.compile(r"^\s*\[Community-posted[^\]]*\]\s*")
TESTER_RESULT_RE = re.compile(r"^(WINNER|NO_WINNER|INCONCLUSIVE):[ \t]*(.*)$", re.MULTILINE)
# A post's first line in the MCP `search` / `get_post` output (plugin/mcp/server.py format_post).
POST_HEADER_RE = re.compile(r"^(SOLVED|OPEN QUESTION) post #(\d+) \|", re.MULTILINE)
# How `post` / `post_open_question` say they saved it (plugin/mcp/server.py). Any other reply (an error, REVISE
# feedback) means nothing was saved, even though Claude Code counts the call as a success.
POSTED_RE = re.compile(r"^(?:Posted as new solved post|Solution attached to open question) #(\d+)")
OQ_POSTED_RE = re.compile(r"^(?:Posted as new open question|Already on the forum as open question) #(\d+)")
# Our own MCP tools: from the plugin (plugin/.mcp.json), or the same server added by hand under the name "breakpoint".
OUR_TOOL_PREFIXES = ("mcp__plugin_breakpoint_breakpoint__", "mcp__breakpoint__")

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
    """Keep this install's identity in sync (model, email, registration). Shows nothing to the agent:
    news about a solved question now goes out by email only (docs/accounts.md), so there is no
    session-start "updates" message any more."""
    if payload.get("source") == "compact":   # same session continuing; nothing changed
        return None
    sync = getattr(backend, "sync_identity", None)   # accounts (docs/accounts.md): model, email, register
    if sync:
        sync(payload)
    return None


def on_user_prompt(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """Signal 6: the user pushes back ("still broken") or pastes an error we already saw."""
    prompt = str(payload.get("prompt") or "")
    notebook.record(state, {"kind": "user", "t": time.time()})
    menu = state.get("pending_menu")
    if menu:   # the stuck menu waits: "1" / "2" / "3" (or the option's words) answers it; anything else drops it
        state["pending_menu"] = None
        typed = prompt.strip()
        choice = _menu_choice(typed, menu.get("options") or [], numbers=True) \
            if len(typed) <= MENU_TYPED_CHARS else None
        if choice:
            text = _menu_answer(payload, state, backend, menu, choice, typed=typed)
            return messages.context("UserPromptSubmit", text) if text else None
    signals = struggles.check_user_message(prompt, state["history"])
    if not struggles.is_struggling(signals):
        return None
    text = _nudge(payload, state, backend, signals, _problem(state, fallback=prompt))
    return messages.context("UserPromptSubmit", text) if text else None


def on_tool_use(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """PostToolUse and PostToolUseFailure: record the action, then decide after commands."""
    event = payload.get("hook_event_name", "PostToolUse")
    if payload.get("tool_name") == ASK_TOOL:
        text = _after_menu_question(payload, state, backend)   # the user's answer to the stuck menu
    elif str(payload.get("tool_name", "")).startswith("mcp__"):
        text = _after_mcp_tool(payload, state)
    else:
        actions, output = notebook.actions_from_tool_use(payload)   # a shell command: its file writes, then its run
        action = actions[-1] if actions else None
        if action is not None and action["kind"] == "run" and _is_tester_command(action["cmd"]):
            text = _after_tester(state, output)   # our own tester: not work on the problem, never a struggle
            return messages.context(event, text) if text else None
        if action is None or (action["kind"] == "run" and _is_rewind_command(action["cmd"])):
            return None   # tools we don't track, and our own rewind.py (not work on the problem)
        for item in actions:
            notebook.record(state, item)
        text = _after_run(payload, state, backend, action, output) if action["kind"] == "run" else None
    return messages.context(event, text) if text else None


def on_pre_tool_use(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """PreToolUse, before every tool call. main.py only gets here when the notes file has a pending_menu (no git, no
    network). Any tool other than the menu's question ends the session and shows the user the menu as text."""
    menu = state.get("pending_menu")
    if not menu:
        return None
    if not state.get("episode"):   # the problem is gone (the command passed): nothing to ask any more
        state["pending_menu"] = None
        return None
    if payload.get("tool_name") in MENU_ALLOWED_TOOLS:
        return None
    menu["stopped"] = True   # the user answers by typing (on_user_prompt)
    return messages.stop_session(messages.typed_menu(menu["question"], menu["options"]))


def on_stop(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """Signals 4 and 5, and the open question reminder. Blocks at most once per episode."""
    if payload.get("stop_hook_active"):
        return None
    episode = state.get("episode")
    menu = state.get("pending_menu")
    replies = assistant_replies(payload.get("transcript_path"), 1)
    if menu and episode:   # the menu waits for the user: never NOT_DONE ("keep fixing") now
        if menu.get("stop_reminded") or (replies and menu["question"] in replies[-1]):
            return None   # already reminded, or the reply shows the menu as text: the user answers by typing
        menu["stop_reminded"] = True
        return messages.block(messages.menu_reminder(messages.typed_menu(menu["question"], menu["options"])))
    if episode and episode.get("stop_blocked"):
        return None
    history = state["history"]
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

    # (a2) The agent read a solved post itself (search / get_post) while this command failed: the pass confirms it.
    # Not when the tester's winning team fix is what made it pass: that is a new solution (NOTABLE_SOLVE).
    viewed = _take_viewed(state, key) if run["ok"] else None
    if viewed and not (episode and episode["key"] == key and episode.get("team_fix")):
        if episode and episode["key"] == key:
            state["episode"] = None   # solved with a posted fix: nothing new to post
        backend.report_outcome(viewed["post_id"], True, viewed.get("fp"), run["cmd"],
                               str(payload.get("session_id", "")))
        return _leave_reply(state, viewed["post_id"], run["cmd"], viewed.get("fp"), viewed=True)

    # (b) The command passes after failing.
    if run["ok"]:
        same_key = bool(episode) and episode["key"] == key
        failures = _failures_before(history, key, idx, episode["started_idx"] if same_key else 0)
        team_fix = episode.get("team_fix") if same_key else None
        question = episode.get("open_question_id") if same_key else None   # kept before the episode closes
        if failures >= NOTABLE_MIN_FAILURES or ((team_fix or question) and failures):
            fp = (_last_failed_run(history[:idx], key) or {}).get("err_fp")
            tried = list(episode["tried"]) if same_key else []
            if same_key:
                state["episode"] = None
            findings = suspect_changes(notebook.project_dir(payload))
            if findings:
                return messages.suspect_fix(run["cmd"], failures, fp, findings)
            return messages.notable_solve(run["cmd"], failures, fp, tried, team_fix, question)
        if same_key and failures:
            state["episode"] = None   # fixed quickly: nothing worth saying
            return None

    # (b2) A failure with an error the forum was not asked about yet: one early lookup, silent unless it matches.
    if not run["ok"]:
        text = _early_lookup(state, backend, run, output)
        if text:
            return text

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
        _take_viewed(state, run["key"])   # the KNOWN_FIX post wins over posts the agent opened itself
        return _leave_reply(state, fix["post_id"], run["cmd"], fix.get("fp") or episode["fp"])
    # It did not work: a later pass is not that post's doing, even if the agent opened it with get_post.
    state["viewed_posts"] = [v for v in state.get("viewed_posts") or [] if v.get("post_id") != fix["post_id"]]
    _add_tried(episode, f"forum fix from post #{fix['post_id']} -> did not work")
    text = messages.fix_failed(fix["post_id"], run["cmd"], run["err_fp"], episode["tried"])
    episode["rung"] = max(episode["rung"], 1)   # forum done; go on to triage now
    if episode.get("early") and not struggles.is_struggling(struggles.check_after_run(state["history"])):
        episode["last_nudge_idx"] = len(state["history"])
        return text   # the fix came from the early check and the agent is not stuck yet: the ladder waits
    problem = (run["cmd"], output, run["err_fp"])
    reason = f"The forum fix from post #{fix['post_id']} did not work."
    next_step = _climb(state, backend, problem, reason, payload, [])   # no signals: no rewind here, as before
    episode["last_nudge_idx"] = len(state["history"])
    return text + "\n\n" + next_step if next_step else text


def _leave_reply(state: dict[str, Any], post_id: Any, command: str, fp: str | None,
                 viewed: bool = False) -> str | None:
    """LEAVE_REPLY for a confirmed forum fix, at most once per post per session."""
    if post_id is None:
        return None
    nudged = state.setdefault("reply_nudged", [])
    if post_id in nudged:
        return None
    nudged.append(post_id)
    return messages.leave_reply(post_id, command, fp, viewed)


def _take_viewed(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The most recent solved post the agent opened while `key` was failing (and forget them all), or None."""
    viewed = [v for v in state.get("viewed_posts") or [] if isinstance(v, dict)]
    mine = [v for v in viewed if v.get("key") == key]
    if mine:
        state["viewed_posts"] = [v for v in viewed if v.get("key") != key]
    return mine[-1] if mine else None


def _note_viewed(payload: dict[str, Any], state: dict[str, Any], get_post: bool) -> None:
    """The agent's own `search` / `get_post` showed a SOLVED post: remember it for the command failing now."""
    key = _failing_key(state["history"])
    if not key:
        return   # nothing is failing: a later pass would prove nothing about this post
    headers = POST_HEADER_RE.findall(notebook.tool_response_text(payload))
    if get_post:   # one post: its header comes first (the replies below it are other agents' text)
        headers = headers[:1]
    post_id = next((int(n) for label, n in headers if label == "SOLVED"), None)   # search: the best solved match
    if post_id is None:
        return
    fp = (_last_failed_run(state["history"], key) or {}).get("err_fp")
    viewed = [v for v in state.get("viewed_posts") or [] if isinstance(v, dict)
              and (v.get("post_id"), v.get("key")) != (post_id, key)]
    viewed.append({"post_id": post_id, "key": key, "fp": fp})
    state["viewed_posts"] = viewed[-notebook.VIEWED_LIMIT:]


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


def our_tool(tool_name: Any) -> str | None:
    """The bare name of one of our MCP tools ("post", "search", ...), or None for any other tool."""
    name = str(tool_name or "")
    return next((name[len(p):] for p in OUR_TOOL_PREFIXES if name.startswith(p)), None)


def _saved_id(payload: dict[str, Any], pattern: re.Pattern[str]) -> int | None:
    """The post id when a post / post_open_question result says it was saved, else None."""
    text = LABEL_RE.sub("", notebook.tool_response_text(payload).replace(messages.COMMUNITY_LABEL, ""), count=1)
    match = pattern.match(text.lstrip())
    return int(match.group(1)) if match else None


def _after_mcp_tool(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Track our own MCP tools: the team was called, an open question was posted, a fix was shared.
    Other servers' tools are ignored, even when they have the same name (another server's `post`)."""
    name = our_tool(payload.get("tool_name"))
    episode = state.get("episode")
    if name in ("ask_for_help", "check_help"):
        return _after_team_tool(payload, state, name == "ask_for_help")
    if name in ("search", "get_post"):
        _note_viewed(payload, state, name == "get_post")
        return None
    if name == "reply":   # it already replied to that post: no LEAVE_REPLY for it later
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        post_id = tool_input.get("post_id")
        if isinstance(post_id, int) and not isinstance(post_id, bool) \
                and "Reply #" in notebook.tool_response_text(payload):
            nudged = state.setdefault("reply_nudged", [])
            if post_id not in nudged:
                nudged.append(post_id)
        return None
    if name == "post_open_question" and episode:
        question = _saved_id(payload, OQ_POSTED_RE)
        if question is not None:   # an error or REVISE: not posted, so the reminders stay on
            episode.update(oq_posted=True, open_question_id=question)
    elif name == "post" and episode and _saved_id(payload, POSTED_RE) is not None:
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
    """ask_for_help / check_help results."""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    return _team_result(payload, state, notebook.tool_response_text(payload), asked,
                        str(tool_input.get("run_id") or ""))


def _team_result(payload: dict[str, Any], state: dict[str, Any], text: str, asked: bool,
                 run_id: str = "") -> str | None:
    """An ask_for_help / check_help result, or the server's answer to the hook's own POST /api/help (_start_team).
    Only TEAM_STARTED (or a later team answer) means the team really ran."""
    tag, rest = _team_reply(text)
    episode = state.get("episode")
    if not tag:
        if asked and episode:
            episode["rung"] = max(episode["rung"], 3)   # it escalated; the reply was an error or unknown
        return None
    if episode is None:   # a direct call the hook did not suggest: tie it to the failing command, if any
        key = _failing_key(state["history"])
        episode = _open_episode(state, key) if key else notebook.new_episode("", None, len(state["history"]))
        state["episode"] = episode
    run_id = run_id or (rest.split() or [""])[0].strip(".,:;") or str(episode.get("team_run_id") or "")
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
    if status is None or at_stop:   # status None: the team ran but gave no known status (safe fallback)
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
    episode["early"] = False   # the agent counts as stuck now: the full ladder applies
    length = len(state["history"])
    if not _cooled_down(state, episode):
        return None
    reason = " ".join(s.reason for s in signals if s.strength == "strong")
    text = _climb(state, backend, problem, reason, payload, signals)
    if text:
        episode["last_nudge_idx"] = length
        episode["nudge_fp"] = [length, problem[2]]   # the error this message was about (for _cooled_down)
    return text


def _cooled_down(state: dict[str, Any], episode: dict[str, Any]) -> bool:
    """May the ladder speak again? Yes after NUDGE_COOLDOWN actions or NUDGE_COOLDOWN_FAILURES failed runs since
    the last message, whichever comes first. Counting failures keeps the pace for an agent that patches and tests
    in one shell call (one action per try). One failure is enough when it shows a different error than the last
    message was about (each fix reveals a new error: the agent is going in circles, not still trying our advice),
    and after a REWIND_OFFER, which is only a question for the user."""
    last = episode.get("last_nudge_idx")
    if last is None:
        return True
    since = state["history"][last:]
    failed = [a for a in since if a.get("kind") == "run" and not a.get("ok")
              and not struggles._is_probe(a.get("key", ""))]
    at, fp = episode.get("nudge_fp") or [None, None]
    moved_on = bool(failed) and at == last and bool(fp) and failed[-1].get("err_fp") not in (None, fp)
    needed = 1 if episode.get("rewind_offer_idx") == last or moved_on else NUDGE_COOLDOWN_FAILURES
    return len(since) >= NUDGE_COOLDOWN or len(failed) >= needed


def _climb(state: dict[str, Any], backend: Any, problem: tuple[str, str, str | None], reason: str,
           payload: dict[str, Any] | None = None, signals: list[struggles.Signal] | None = None) -> str | None:
    episode = state["episode"]
    command, error, fp = problem
    tried = episode["tried"]

    if episode["rung"] < 1:
        episode["rung"] = 1
        _note_looked_up(state, fp)
        top = _forum_match(backend.search(error, command, fp))
        if top:
            return _forum_message(episode, top, command, fp)
        # no match, or the server is down: go on to triage now

    mode = settings.team_mode()
    if episode["rung"] < 3 and payload is not None:   # the rewind sits between the forum and the team
        options = _rewind_options(payload, state, signals or [])
        if options and mode == "ask":
            return _stuck_menu(state, problem, reason, options)   # one menu for the rewind and the team
        if options:
            return _rewind_offer(state, options, problem, reason)

    if episode["rung"] < 2:
        episode["rung"] = 2
        kind, why = classify(error)
        if kind == "own_code":
            return messages.step_back(command, fp, tried, why)

    if episode["rung"] < 3:
        episode["rung"] = 3
        if mode == "off":
            return None   # never start or suggest the team
        if mode == "auto":
            return _start_team(payload, state, backend, problem, reason, "team_mode is auto: the hook starts the "
                                                                         "team by itself when the agent is stuck.")
        return _stuck_menu(state, problem, reason, None)

    if episode["rung"] < 4 and _open_question_due(state, episode):
        episode["rung"] = 4
        return messages.post_open_question(command, fp, tried, reason)
    return None


# ---------------------------------------------------------------------------
# The stuck menu (team_mode ask) and starting the team from the hook
# ---------------------------------------------------------------------------

def _stuck_menu(state: dict[str, Any], problem: tuple[str, str, str | None], reason: str,
                rewind: list[dict[str, Any]] | None) -> str:
    """STUCK_MENU, once per episode: the team step is reached (and the rewind offered, if there is one)."""
    episode = state["episode"]
    command, error, fp = problem
    episode["rung"] = 3
    options = ["team"] + (["rewind"] if rewind else []) + ["keep"]
    note = None
    if rewind:
        episode["rewind_offered"] = True
        chosen = _rewind_choice(rewind)
        note = f"Back to {chosen['title']} ({chosen['time']}); undoes {chosen['edits']} edits."
    failures, errors = _failure_counts(state, episode["key"] or struggles.command_key(command))
    question = messages.menu_question(command, failures, errors)
    state["pending_menu"] = {"question": question, "options": options, "problem": [command, error, fp],
                             "reason": reason, "rewind": rewind, "idx": len(state["history"]), "t": time.time()}
    return messages.stuck_menu(question, options, command, fp, episode["tried"], reason, note)


def _rewind_choice(options: list[dict[str, Any]]) -> dict[str, Any]:
    """The rewind the menu means ("to before the problem started"): episode_start if there is one, else the first."""
    return next((o for o in options if o["name"] == "episode_start"), options[0])


def _failure_counts(state: dict[str, Any], key: str) -> tuple[int, int]:
    """(failed runs, different errors) of `key` in the open episode (or the whole history)."""
    episode = state.get("episode") or {}
    start = episode.get("started_idx", 0) if episode.get("key") == key else 0
    runs = [a for a in state["history"][start:] if _is_run(a, key) and not a.get("ok")]
    errors = len({a.get("err_fp") for a in runs if a.get("err_fp")})
    return len(runs), max(errors, 1 if runs else 0)


def _menu_choice(text: str, options: list[str], numbers: bool = False) -> str | None:
    """Which menu option a text names ("team", "rewind" or "keep"), or None. `numbers`: also "1" / "2" / "3" (the
    typed menu's numbering). Words must point at exactly one option (a text with every label means nothing)."""
    if numbers:
        match = MENU_NUMBER_RE.match(text)
        if match:
            n = int(match.group(1))
            return options[n - 1] if 1 <= n <= len(options) else None
    lower = text.lower()
    found = [name for name in options if any(word in lower for word in MENU_KEYWORDS.get(name, ()))]
    return found[0] if len(found) == 1 else None


def _asked_answer(payload: dict[str, Any]) -> str:
    """The user's answer in an AskUserQuestion result: its `answers` ({question: label}), else the
    "question"="answer" pairs of its text form, else the whole text (then _menu_choice must find one label only)."""
    response = payload.get("tool_response")
    answers = response.get("answers") if isinstance(response, dict) else None
    if isinstance(answers, dict) and answers:
        return " | ".join(str(v) for v in answers.values())
    if isinstance(answers, list) and answers:
        return " | ".join(str(v) for v in answers)
    text = notebook.tool_response_text(payload)
    pairs = ANSWER_PAIR_RE.findall(text)
    return " | ".join(pairs) if pairs else text.strip()


def _after_menu_question(payload: dict[str, Any], state: dict[str, Any], backend: Any) -> str | None:
    """The AskUserQuestion result while the stuck menu waits."""
    menu = state.get("pending_menu")
    if not menu or payload.get("hook_event_name") == "PostToolUseFailure":
        return None   # no menu, or the question failed: the menu waits (the next other tool call shows it as text)
    answer = _asked_answer(payload)
    if not answer:
        return None
    choice = _menu_choice(answer, menu.get("options") or [])
    if choice:
        return _menu_answer(payload, state, backend, menu, choice)
    state["pending_menu"] = None   # the user wrote an answer of their own ("Other"), or it can't be read
    command, _, fp = menu["problem"]
    return messages.team_status(
        "Breakpoint could not match the user's answer to one of its menu options.",
        "do what the user said. If they asked for the specialist team, call `ask_for_help` yourself (see CALL_TEAM "
        "in the breakpoint skill); otherwise keep working.", command, fp)


def _menu_answer(payload: dict[str, Any], state: dict[str, Any], backend: Any, menu: dict[str, Any], choice: str,
                 typed: str = "") -> str | None:
    """Act on the user's menu answer in the hook itself. `typed`: what the user typed (the agent did not see the
    question then, so even "keep trying" gets a line)."""
    state["pending_menu"] = None
    episode = state.get("episode")
    if not episode:
        return None   # the episode closed meanwhile: nothing left to do
    command, error, fp = menu["problem"]
    episode["last_nudge_idx"] = len(state["history"])   # the ladder's cooldown counts from the answer
    label = messages.MENU_OPTIONS[choice][0]
    if choice == "team":
        text = _start_team(payload, state, backend, (command, error, fp), menu.get("reason") or "",
                           f"the user picked \"{label}\" in the Breakpoint menu.")
    elif choice == "rewind" and menu.get("rewind"):
        episode["rewind_offer_idx"] = len(state["history"])   # one failure after it is enough to climb again
        text = messages.rewind_offer(menu["rewind"], command, fp, episode["tried"], menu.get("reason") or "",
                                     _rewind_choice(menu["rewind"])["name"])
    else:
        text = messages.keep_trying(command, fp, episode["tried"]) if typed else None
    if text and typed:
        text += f"\n\n(The user typed \"{typed}\" to answer the Breakpoint menu: \"{label}\".)"
    return text


def _start_team(payload: dict[str, Any] | None, state: dict[str, Any], backend: Any,
                problem: tuple[str, str, str | None], reason: str, why: str) -> str | None:
    """Send POST /api/help from the hook (the same case file as `ask_for_help`) and read the answer the same way.
    TEAM_STARTED -> TEAM_CALLED. A request that fails -> CALL_TEAM, so the agent calls ask_for_help itself."""
    payload = payload or {}
    episode = state["episode"]
    command, error, fp = problem
    episode["rung"] = max(episode["rung"], 3)
    ask = getattr(backend, "ask_help", None)
    reply = None
    if ask is not None:
        try:
            reply = ask(_case_file(payload, state, problem, reason, why))
        except Exception:
            reply = None
    tag, _ = _team_reply(reply or "")
    if not tag:   # the server is down, said no, or gave an answer without a tag
        return messages.call_team(command, error, fp, episode["tried"], reason, episode.get("open_question_id"),
                                  note="Breakpoint tried to start the team itself, but the request failed: call "
                                       "`ask_for_help` yourself now, as below.")
    text = _team_result(payload, state, reply or "", asked=True)
    if tag in ("TEAM_STARTED", "TEAM_WORKING"):
        return messages.team_called(str(episode.get("team_run_id") or ""), command, fp, episode["tried"], why)
    if tag in ("KNOWN_FIX", "TRY_FIRST"):
        return messages.team_answered(reply or "", command, fp, episode["tried"], why)
    return text   # TEAM_FAILED (TEAM_STATUS: keep working), or a finished run (TEST_FIXES, POST_OPEN_QUESTION)


def _case_file(payload: dict[str, Any], state: dict[str, Any], problem: tuple[str, str, str | None], reason: str,
               why: str) -> dict[str, Any]:
    """The hook's own case file for POST /api/help: what the hook knows (the command, the error, what was tried, the
    files edited most recently), plus the environment and `git diff` (case_builder.py, as `ask_for_help` adds)."""
    episode = state["episode"]
    command, error, fp = problem
    failures, errors = _failure_counts(state, episode["key"] or struggles.command_key(command))
    edited = [a.get("path") for a in reversed(state["history"]) if a.get("kind") == "edit"]
    files = [p for p in dict.fromkeys(edited) if p and p != "<script>"][:TEAM_FILES]
    description = (f"`{command}` keeps failing ({failures} failed runs; different errors: {errors}) and the forum has "
                   f"no fix. Sent by the Breakpoint hook, not written by the agent: {why} Why the hook counts the "
                   f"agent as stuck: {reason or 'repeated failures.'}")
    try:
        return case_builder.build(description, command, error, episode["tried"], fp or "", "", "", "", files,
                                  project_id=str(state.get("project_id") or ""),
                                  project=notebook.project_dir(payload))
    except Exception:   # never lose the request: send what the hook knows
        return {"goal": description, "symptom": error or description, "check_command": command,
                "tried": case_builder.parse_tried(episode["tried"]), "fingerprint": fp or "",
                "project": str(state.get("project_id") or "")}


def _rewind_options(payload: dict[str, Any], state: dict[str, Any],
                    signals: list[struggles.Signal]) -> list[dict[str, Any]] | None:
    """The rewind options when one is worth offering (once per episode, when the agent goes in circles with 3+ edits
    since a checkpoint), else None. Changes nothing: REWIND_OFFER or the stuck menu offers them."""
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
    return options or None


def _rewind_offer(state: dict[str, Any], options: list[dict[str, Any]], problem: tuple[str, str, str | None],
                  reason: str) -> str:
    """REWIND_OFFER (team_mode auto or off; in ask mode the stuck menu offers the rewind)."""
    episode = state["episode"]
    episode["rewind_offered"] = True
    episode["rewind_offer_idx"] = len(state["history"])   # = last_nudge_idx after this message: next failure may climb
    command, _, fp = problem
    return messages.rewind_offer(options, command, fp, episode["tried"], reason)


def _early_lookup(state: dict[str, Any], backend: Any, run: dict[str, Any], output: str) -> str | None:
    """The early forum check: one search for an error fingerprint this session has not looked up yet, before the
    agent counts as stuck. KNOWN_FIX / OPEN_QUESTION_EXISTS on a close match (opening the episode, rung 1 done),
    else None, and the ladder later behaves as it always did."""
    fp = run["err_fp"]
    if not fp or fp in (state.get("looked_up") or []):
        return None
    episode = state.get("episode")
    if episode and episode["key"] != run["key"]:
        return None   # the open episode is about another command: don't mix them up
    if episode and not _cooled_down(state, episode):
        return None   # we spoke very recently; this error can still be looked up at its next failure
    _note_looked_up(state, fp)
    top = _forum_match(backend.search(output, run["cmd"], fp))
    if not top:
        return None   # no close match, or the server is down: say nothing
    if episode is None:
        episode = _open_episode(state, run["key"])
        episode["early"] = True   # opened by this check, not because the agent is stuck
    episode["rung"] = max(episode["rung"], 1)   # the forum step is done: the ladder won't search again
    episode["last_nudge_idx"] = len(state["history"])
    return _forum_message(episode, top, run["cmd"], fp)


def _note_looked_up(state: dict[str, Any], fp: str | None) -> None:
    """Remember that the forum was asked about this fingerprint (at most once per session for the early check)."""
    looked_up = [f for f in state.get("looked_up") or [] if f != fp]
    if fp:
        state["looked_up"] = (looked_up + [fp])[-notebook.LOOKED_UP_LIMIT:]


def _forum_match(results: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The top search result when it is a close solved post with a fix or a close open question, else None."""
    top = results[0] if results else None
    if not top or not _close_match(top):
        return None
    if (top.get("status") == "solved" and top.get("solution")) or top.get("status") == "open":
        return top
    return None


def _forum_message(episode: dict[str, Any], top: dict[str, Any], command: str, fp: str | None) -> str:
    """KNOWN_FIX (the next run of the command is its test) or OPEN_QUESTION_EXISTS for a _forum_match post."""
    if top.get("status") == "solved":
        episode["pending_fix"] = {"post_id": top.get("id"), "key": struggles.command_key(command), "fp": fp}
        return messages.known_fix(top, command, fp, episode["tried"])
    episode["open_question_id"] = top.get("id")
    return messages.open_question_exists(top, command, fp, episode["tried"])


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
    """The last JUDGE_ACTIONS actions for POST /api/judge: {tool, summary, ok, out}, plus the raw
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
