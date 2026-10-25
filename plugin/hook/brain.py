"""Brain: what the hook decides at each event (PROJECT_PLAN.md, docs/struggle-signals.md).

One handler per Claude Code event. Each takes (payload, state, backend) and returns the JSON to
print, or None to stay quiet. `state` is the notebook (notebook.py) and is changed in place; main.py
saves it. `backend` is the backend module, passed in so tests can use a fake one.

The help ladder. When struggles.py says the agent is struggling, the open episode climbs one rung
and the agent gets one message (at most once per NUDGE_COOLDOWN actions):
  1 FORUM   search the forum. A solved, close match -> KNOWN_FIX (we then watch the next run of that
            command and report the outcome). An open question -> OPEN_QUESTION_EXISTS. Nothing, or
            the server is down -> go straight to rung 2.
  2 TRIAGE  is the error in the outside world (a library, tool, environment) or the project's own
            code? Own code -> STEP_BACK checklist once. External -> go straight to rung 3.
  3 TEAM    CALL_TEAM once: ask_for_help (server mode) or run_specialists (local mode).
  4 OQ      the team ran and the agent is still stuck -> POST_OPEN_QUESTION.

A command that kept failing and now passes closes the episode: NOTABLE_SOLVE, or SUSPECT_FIX when
the git diff shows the failure was hidden (|| true, skipped tests, ...) instead of fixed.
The backend is called only at these decision points, never on every event.
"""

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

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
        if action is None:
            return None
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
    elif episode and episode.get("team_ran") and not episode.get("oq_posted"):
        command, _, fp = _problem(state)
        episode["rung"] = 4
        text = messages.post_open_question(command, fp, episode["tried"],
                                           "The team was called, but the problem was never confirmed fixed.")
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
    if not run["ok"]:
        state["last_error"] = {"cmd": run["cmd"], "key": key, "fp": run["err_fp"],
                               "text": output[-notebook.ERROR_CHARS:]}
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
        if failures >= NOTABLE_MIN_FAILURES:
            fp = (_last_failed_run(history[:idx], key) or {}).get("err_fp")
            tried = list(episode["tried"]) if same_key else []
            if same_key:
                state["episode"] = None
            findings = suspect_changes(notebook.project_dir(payload))
            if findings:
                return messages.suspect_fix(run["cmd"], failures, fp, findings)
            return messages.notable_solve(run["cmd"], failures, fp, tried)
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
    if "ask_for_help" in name or "run_specialists" in name:
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
            command, _, fp = _problem(state)
            episode["last_nudge_idx"] = len(state["history"])
            return messages.post_open_question(command, fp, episode["tried"],
                                               "The specialist team found no verified fix.")
    elif "post_open_question" in name and episode:
        episode["oq_posted"] = True
    elif name.endswith("__post") and episode:
        state["episode"] = None   # solved and shared
    return None


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
    text = _climb(state, backend, problem, reason)
    if text:
        episode["last_nudge_idx"] = length
    return text


def _climb(state: dict[str, Any], backend: Any, problem: tuple[str, str, str | None], reason: str) -> str | None:
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

    if episode["rung"] < 2:
        episode["rung"] = 2
        kind, why = classify(error)
        if kind == "own_code":
            return messages.step_back(command, fp, tried, why)

    if episode["rung"] < 3:
        episode["rung"] = 3
        return messages.call_team(backend.team_mode(), command, error, fp, tried, reason,
                                  episode.get("open_question_id"))

    if episode["team_ran"] and not episode["oq_posted"] and episode["rung"] < 4:
        episode["rung"] = 4
        return messages.post_open_question(command, fp, tried, reason)
    return None


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
