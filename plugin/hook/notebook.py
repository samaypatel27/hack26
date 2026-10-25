"""Notebook: the hook's memory for one Claude Code session (PROJECT_PLAN.md, "The stuck engine").

Every hook event is a fresh Python process, so anything the hook needs to remember between events
lives in a small JSON file:

  <project dir>/.claude/loop_state/<session_id>.json      (project dir = $CLAUDE_PROJECT_DIR or the payload's cwd)

The folder gets a .gitignore containing `*` so this never shows up in the user's git diff. Writes are
atomic (temp file + os.replace) so a crash mid-write can never leave half a file.

The state file holds:
  project_id   sha1 of the git remote URL (or of the folder path), first 12 hex chars
  history      the last HISTORY_LIMIT actions, in the shapes documented in struggles.py
  episode      the open stuck episode, or None (see new_episode)
  last_judge   history length when the AI judge last ran
  last_error   the most recent failing command and its error text, for forum searches
  goal_key     the command the last stuck episode was about (its pass saves a "last_good" checkpoint)
  checkpoints  git snapshots saved this session, oldest first (checkpoints.py, docs/rewind.md):
               {"id", "ref", "label", "time", "history_idx", "key", "cmd"}. Two entries can share one
               snapshot when the files did not change in between.
  viewed_posts solved posts the agent opened itself with the `search` / `get_post` MCP tools while a command was
               failing, oldest first: {"post_id", "key", "fp"} (key = that failing command). If the command
               then passes, the brain treats it like a confirmed forum fix (outcome + LEAVE_REPLY).
  reply_nudged post ids LEAVE_REPLY was sent for (or the agent already replied to): at most once per post
  looked_up    error fingerprints the forum was already searched for this session (brain.py's early forum check
               runs once per fingerprint), oldest first
  pending_menu the stuck menu waiting for the user's answer (team_mode ask, brain.py), or None:
               {"question", "options" (names: team, rewind, keep), "problem" [command, error, fp], "reason",
               "rewind" (the rewind options, or None), "idx", "t", "stopped" (the session was ended to show it)}

This file also turns raw hook payloads into history records (action_from_tool_use).
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import struggles

STATE_DIR = Path(".claude") / "loop_state"
HISTORY_LIMIT = 200
TAIL_CHARS = 500        # output kept per run, for the AI judge and "already tried" notes
ERROR_CHARS = 2000      # error text kept for forum searches and messages
GIT_TIMEOUT = 2
VIEWED_LIMIT = 10      # viewed_posts entries kept
LOOKED_UP_LIMIT = 50    # looked_up fingerprints kept
CHECKPOINT_LIMIT = 20   # entries kept in the notes (checkpoints.py keeps the same number of git refs)
REWIND_KIND = "rewind"  # history record written by rewind.py; struggles.py counts from the last one

SHELL_TOOLS = {"Bash", "PowerShell"}   # both run a command; PowerShell is Claude Code's shell tool on Windows
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
EXIT_CODE_KEYS = ("exit_code", "exitCode", "returncode", "returnCode", "exitStatus")
BACKGROUND_KEYS = ("backgroundTaskId", "background_task_id", "backgroundedByUser")

# `pytest | tail` exits 0 even when pytest failed, so a piped command's output is checked too.
PIPED_FAILURE_RE = re.compile(
    r"\b[1-9]\d* failed\b|^FAILED|Traceback \(most recent call last\)|^npm ERR!|^Error:",
    re.MULTILINE,
)

# A test runner's own summary of the run (unittest, pytest, cargo, go, jest/vitest, npm, tsc).
CHECK_FAIL_RE = re.compile(
    r"^FAILED \(|\b[1-9]\d* (?:failed|errors?)\b|^FAIL[: ]|test result: FAILED|^Tests:.*\b[1-9]\d* failed"
    r"|npm ERR!|\berror TS\d+|^error(?:\[E\d+\])?:|\bERROR collecting\b|\berrors? during collection\b",
    re.MULTILINE,
)
CHECK_PASS_RE = re.compile(r"^OK\b|\b\d+ passed\b|test result: ok|^ok\s|^Tests:\s+\d+ passed|^PASS\b",
                           re.MULTILINE)
GENERIC_FAIL_RE = re.compile(r"Traceback \(most recent call last\)|^\w*Error:|^Error:", re.MULTILINE)
GREP_LIKE = {"grep", "rg", "findstr", "select-string", "sls"}   # exit 1 when nothing matched
HEAD_LIKE = {"head", "select-object", "select", "more", "less"}   # may cut off the summary at the end

# Shell commands that write files (docs/hook-engine.md, "Edits made through the shell").
SED_INPLACE_RE = re.compile(r"^(?:sed|gsed)$")
PS_WRITE_CMDS = {"set-content", "add-content", "out-file", "sc", "ac"}
SCRIPT_WRITE_RE = re.compile(r"""open\([^)]*?,\s*(?:mode\s*=\s*)?['"][wax]|\.write_(?:text|bytes)\(|writeFileSync\(""")
SCRIPT_RUNNER_RE = re.compile(r"^(?:python[\d.]*|py|python3|node)(?:\.exe)?$", re.IGNORECASE)
PATH_LITERAL_RE = re.compile(r"""['"]([\w./\:-]+\.[A-Za-z0-9]{1,6})['"]""")
NULL_TARGETS = {"/dev/null", "nul", "$null", "nul:"}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flash on Windows


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------

def project_dir(payload: dict[str, Any]) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())


def state_dir(payload: dict[str, Any]) -> Path:
    return project_dir(payload) / STATE_DIR


def ensure_state_dir(payload: dict[str, Any]) -> Path:
    """Create the state folder and its `*` .gitignore on first use."""
    folder = state_dir(payload)
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return folder


def state_path(payload: dict[str, Any]) -> Path:
    session = re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("session_id") or "unknown"))
    return state_dir(payload) / f"{session}.json"


def project_id(folder: Path) -> str:
    """Same ID for every clone of a repo (hash of the git remote), else a hash of the folder path."""
    source = str(folder.resolve())
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"], cwd=folder, capture_output=True,
            text=True, timeout=GIT_TIMEOUT, creationflags=NO_WINDOW,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            source = result.stdout.strip()
    except Exception:
        pass
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Load and save
# ---------------------------------------------------------------------------

def new_state(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": payload.get("session_id", ""),
        "project_id": project_id(project_dir(payload)),
        "history": [],
        "episode": None,
        "last_judge": None,
        "last_error": None,
        "goal_key": None,
        "checkpoints": [],
        "viewed_posts": [],
        "reply_nudged": [],
        "looked_up": [],
        "pending_menu": None,
    }


def new_episode(key: str, fp: str | None, started_idx: int) -> dict[str, Any]:
    """One stuck episode: from the first sign of trouble until the command passes."""
    return {
        "key": key,                 # the command it is about (struggles.command_key)
        "fp": fp,                   # latest error fingerprint for that command
        "rung": 0,                  # help ladder step reached: 1 forum, 2 triage, 3 team, 4 open question
        "pending_fix": None,        # {"post_id", "key", "fp"} while a forum fix waits to be tested
        "open_question_id": None,   # set when the forum has an open question for this error
        "team_ran": False,
        "oq_posted": False,
        "stop_blocked": False,
        "last_nudge_idx": None,     # history length when we last sent a ladder message
        "rewind_offer_idx": None,   # history length when REWIND_OFFER was sent (brain._cooled_down)
        "started_idx": started_idx,
        "tried": [],                # short notes of what was already tried
        "rewind_offered": False,    # REWIND_OFFER is sent at most once per episode
        "rewound": False,           # set by rewind.py after the user said yes
        # The backup team and the local tester (docs/local-tester.md). Older notes files lack these: use .get().
        "team_run_id": None,        # from TEAM_STARTED: <run_id>
        "team_status": None,        # working | report | no_answer | failed | winner | no_winner
        "team_report_idx": None,    # history length when TEAM_REPORT arrived
        "tester_offered": None,     # run id TEST_FIXES was sent for (once per run)
        "team_fix": None,           # {"hypothesis", "cause", "run_id"}: the tester's WINNER, until the check passes
        "early": False,             # opened by the early forum check, before the agent counted as stuck
    }


def load(payload: dict[str, Any]) -> dict[str, Any]:
    """This session's state, or a fresh one if there is none yet (or the file is unreadable)."""
    try:
        state = json.loads(state_path(payload).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict) or not isinstance(state.get("history"), list):
        return new_state(payload)
    for key in ("episode", "last_judge", "last_error", "goal_key", "pending_menu"):
        state.setdefault(key, None)
    for key in ("checkpoints", "viewed_posts", "reply_nudged", "looked_up"):   # older notes files lack the last three
        if not isinstance(state.get(key), list):
            state[key] = []
    if not state.get("project_id"):
        state["project_id"] = project_id(project_dir(payload))
    return state


def peek(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The notes file as it is, or None: no defaults and no git (PreToolUse runs before every tool call)."""
    try:
        state = json.loads(state_path(payload).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def save(payload: dict[str, Any], state: dict[str, Any]) -> None:
    folder = ensure_state_dir(payload)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, state_path(payload))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(state: dict[str, Any], action: dict[str, Any]) -> None:
    """Append an action, keeping only the last HISTORY_LIMIT. Saved indexes move with the trim."""
    history = state["history"]
    history.append(action)
    extra = len(history) - HISTORY_LIMIT
    if extra <= 0:
        return
    del history[:extra]
    if state.get("last_judge") is not None:
        state["last_judge"] -= extra
    episode = state.get("episode")
    if episode:
        episode["started_idx"] = max(0, episode["started_idx"] - extra)
        for name in ("last_nudge_idx", "rewind_offer_idx"):
            if episode.get(name) is not None:
                episode[name] -= extra
    for entry in state.get("checkpoints") or []:
        entry["history_idx"] = entry.get("history_idx", 0) - extra   # may go below 0: saved before the oldest action we still have


# ---------------------------------------------------------------------------
# Checkpoints (the git side is in checkpoints.py)
# ---------------------------------------------------------------------------

def add_checkpoint(state: dict[str, Any], saved: dict[str, Any], label: str, history_idx: int,
                   run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Note a checkpoints.save() result under `label`. Forgets entries whose git ref was pruned."""
    pruned = set(saved.get("pruned") or [])
    entries = [e for e in state.get("checkpoints") or [] if e.get("ref") not in pruned]
    entry = {"id": saved["id"], "ref": saved["ref"], "label": label, "time": saved.get("time") or time.time(),
             "history_idx": history_idx, "key": (run or {}).get("key", ""), "cmd": (run or {}).get("cmd", "")}
    entries.append(entry)
    state["checkpoints"] = entries[-CHECKPOINT_LIMIT:]
    return entry


def checkpoint_for(state: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The checkpoint a name means: "last_good" / "before_rewind" (the latest one), "episode_start"
    (the one saved at the open episode's first failure, else the latest), or an id number."""
    entries = state.get("checkpoints") or []
    if str(name).isdigit():
        return next((e for e in reversed(entries) if e.get("id") == int(name)), None)
    episode = state.get("episode")
    if name == "episode_start" and episode:
        start = episode.get("started_idx", 0)
        return next((e for e in reversed(entries) if e.get("label") == name and e.get("key") == episode.get("key")
                     and (e.get("history_idx") == start or (start == 0 and e.get("history_idx", 0) <= 0))), None)
    return next((e for e in reversed(entries) if e.get("label") == name), None)


def last_rewind_idx(history: list[dict[str, Any]]) -> int:
    """Index of the last rewind record, or -1."""
    return next((i for i in range(len(history) - 1, -1, -1) if history[i].get("kind") == REWIND_KIND), -1)


def edits_since(state: dict[str, Any], entry: dict[str, Any]) -> tuple[int, list[str]]:
    """(number of edits, files edited) since a checkpoint, not counting edits a rewind already undid."""
    history = state["history"]
    start = max(entry.get("history_idx", -1), last_rewind_idx(history)) + 1
    edits = [a.get("path", "") for a in history[max(start, 0):] if a.get("kind") == "edit"]
    return len(edits), list(dict.fromkeys(p for p in edits if p))


# ---------------------------------------------------------------------------
# Payload -> history record
# ---------------------------------------------------------------------------

def _tail(output: str) -> str:
    """The last TAIL_CHARS of output, starting at a line boundary."""
    if len(output) <= TAIL_CHARS:
        return output
    tail = output[-TAIL_CHARS:]
    return tail.split("\n", 1)[1] if "\n" in tail else tail


def run_record(cmd: str, ok: bool, output: str, t: float | None = None) -> dict[str, Any]:
    return {
        "kind": "run", "t": time.time() if t is None else t, "cmd": cmd,
        "key": struggles.command_key(cmd), "ok": ok,
        "check": struggles.check_index(struggles.split_command(cmd)) is not None,
        "err_fp": None if ok else struggles.fingerprint(output),
        "out_fp": struggles.fingerprint(output, errors_only=False),
        "tail": _tail(output),
    }


def looks_failed(cmd: str, output: str) -> bool:
    """A piped command can exit 0 while the command before the pipe failed."""
    return "|" in cmd and bool(PIPED_FAILURE_RE.search(output))


def output_verdict(output: str) -> bool | None:
    """Did the test/build run pass, judging only by its printed summary? True, False, or None (can't tell).
    A runner's own summary line wins over generic signs like a traceback (a patch script in the same
    command can print one of those)."""
    if CHECK_FAIL_RE.search(output or ""):
        return False
    if CHECK_PASS_RE.search(output or ""):
        return True
    if GENERIC_FAIL_RE.search(output or ""):
        return False
    return None


def check_result(cmd: str, exit_ok: bool, output: str) -> bool | None:
    """Pass/fail of a shell command, or None when it can't be known.

    A compound command's exit code belongs to its last part. When the check (test/build runner) is not the
    last part (`python -m unittest; git status`), its result comes from its output; if the output doesn't
    say, it is unknown, never a pass. `check && other` exiting 0 means the check passed too."""
    segments = struggles.split_command(cmd)
    idx = struggles.check_index(segments)
    if idx is None:   # no test/build runner: the exit code (and the old piped-output check)
        return exit_ok and not looks_failed(cmd, output)
    rest = segments[idx:]
    tail = len(rest) - 1
    piped_only = all(seg.sep == "|" for seg in rest[:-1])       # `check | tail -20` and nothing after
    if tail == 0:
        return exit_ok
    verdict = output_verdict(output)
    if verdict is not None:
        return verdict
    if piped_only:
        last = (struggles.words_of(rest[-1].text) or [""])[0].lower()
        if exit_ok:
            # `pytest | tail` exited 0 and printed no failure (as before). `pytest | head -5` may have cut the
            # summary off, and the exit code is head's: unknown, never a pass.
            return None if last in HEAD_LIKE else True
        return None if last in GREP_LIKE else False
    if exit_ok and all(seg.sep in ("&&", "|") for seg in rest[:-1]):
        return True              # `check && other` only exits 0 when the check passed
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):     # MCP-style content blocks
        return "\n".join(_text(v.get("text") if isinstance(v, dict) else v) for v in value)
    if isinstance(value, dict):
        if isinstance(value.get("content"), list):   # an MCP result object: {"content": [blocks], ...}
            return _text(value["content"])
        if isinstance(value.get("result"), str):
            return value["result"]
        return json.dumps(value)
    return str(value)


def _true(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def result_unknown(payload: dict[str, Any]) -> bool:
    """A command whose result we can't know: started in the background (it reports right away, before it ran),
    or stopped by the user. Counting it as a pass could confirm a fix that never ran, so it is never recorded."""
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and _true(tool_input.get("run_in_background")):
        return True
    if _true(payload.get("is_interrupt")):   # PostToolUseFailure: the user stopped it
        return True
    response = payload.get("tool_response")
    return isinstance(response, dict) and (_true(response.get("interrupted"))
                                           or any(response.get(k) for k in BACKGROUND_KEYS))


def _exit_code(response: dict[str, Any]) -> int:
    """The exit code under whichever key the tool used; 0 when there is none (the event itself says it passed)."""
    code = next((response[k] for k in EXIT_CODE_KEYS if response.get(k) is not None), 0)
    try:
        return int(code)
    except (TypeError, ValueError):
        return 1 if code else 0


def bash_result(payload: dict[str, Any]) -> tuple[str, bool, str]:
    """(command, exit code was 0?, output) for a Bash or PowerShell PostToolUse or PostToolUseFailure payload."""
    tool_input = payload.get("tool_input") or {}
    cmd = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
    if payload.get("hook_event_name") == "PostToolUseFailure":
        return cmd, False, _text(payload.get("error"))     # "Exit code 1\n<output>"

    response = payload.get("tool_response")
    if isinstance(response, dict):
        parts = [_text(response.get(k)) for k in ("stdout", "stderr", "output")]
        output = "\n".join(p for p in parts if p)
        ok = not _exit_code(response)
    else:
        output, ok = _text(response), True
    return cmd, ok, output   # the exit code only; check_result() reads the output too


def _script_writes(body: str) -> list[str]:
    """Files a Python/Node script writes (`open(p, 'w')`, `Path(x).write_text(...)`), as best we can tell.
    Paths come from string literals in the write call or assigned to the name it writes to; a write whose
    path we can't read counts as one edit of "<script>"."""
    if not SCRIPT_WRITE_RE.search(body or ""):
        return []
    paths: list[str] = []
    for match in re.finditer(r"""(?:open|Path|writeFileSync)\(\s*([A-Za-z_]\w*|['"][^'"]+['"])""", body):
        arg = match.group(1)
        if arg[0] in "'\"":
            paths.append(arg[1:-1])
        else:   # a variable: the literals assigned to it (`p = 'recordkit/model.py'`)
            paths += re.findall(rf"""\b{re.escape(arg)}\s*=\s*['"]([^'"]+)['"]""", body)
    paths = [p for p in dict.fromkeys(paths) if PATH_LITERAL_RE.fullmatch(f"'{p}'")]
    return paths or ["<script>"]


def shell_edits(cmd: str) -> list[str]:
    """Files a shell command writes: `python - <<EOF` / `python -c` scripts that open files for writing,
    `sed -i`, `Set-Content` / `Add-Content` / `Out-File`, `>` / `>>` redirects (also `cat > f <<EOF`) and `tee`.
    /dev/null and friends don't count. Paths are as written in the command."""
    text, bodies = struggles.split_heredocs(cmd)
    paths: list[str] = []
    segments = struggles.split_command(cmd)
    body_iter = iter(bodies)
    for seg in segments:
        words = struggles.words_of(seg.text)
        head = (words[0].lower() if words else "")
        uses_heredoc = bool(struggles.HEREDOC_RE.search(seg.text) or struggles.HERESTRING_START_RE.search(seg.text))
        body = next(body_iter, "") if uses_heredoc else ""
        if words and SCRIPT_RUNNER_RE.match(words[0]):
            script = body
            if "-c" in words:
                script = words[words.index("-c") + 1] if words.index("-c") + 1 < len(words) else ""
            elif "-e" in words:
                script = words[words.index("-e") + 1] if words.index("-e") + 1 < len(words) else ""
            paths += _script_writes(script)
        elif SED_INPLACE_RE.match(head) and any(w == "-i" or w.startswith("-i") or w == "--in-place" for w in words):
            scripts = {n + 1 for n, w in enumerate(words) if w in ("-e", "--expression")}
            files = [w for n, w in enumerate(words[1:], 1) if not w.startswith("-") and n not in scripts]
            if not scripts and files:
                files = files[1:]   # the first one is the sed script itself
            paths += files
        elif head in PS_WRITE_CMDS:
            rest = words[1:]
            named = [rest[n + 1] for n, w in enumerate(rest[:-1]) if w.lower() in ("-path", "-filepath", "-literalpath")]
            positional = [w for w in rest if not w.startswith("-")]
            paths += named or positional[:1]
        elif head == "tee":
            paths += [w for w in words[1:] if not w.startswith("-")]
        paths += _redirect_targets(seg.text)
    return [p for p in dict.fromkeys(paths) if p and p.lower() not in NULL_TARGETS]


def _redirect_targets(segment: str) -> list[str]:
    """`> file`, `>> file`, `>file`, `1> file` (not `2>`, `2>&1`, `&>`), outside quotes."""
    try:
        raw = shlex.split(segment.replace("\\", "/"), posix=True)
    except ValueError:
        raw = segment.split()
    targets = []
    for n, w in enumerate(raw):
        m = re.match(r"^(1?>>?)(.*)$", w)
        if not m or w.startswith(">&") or "&" in m.group(2)[:1]:
            continue
        target = m.group(2) or (raw[n + 1] if n + 1 < len(raw) else "")
        if target and not target.startswith("&"):
            targets.append(target)
    return targets


def actions_from_tool_use(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """The history records for a tool call, oldest first, and the run output. A shell command gives an "edit"
    record for each file it writes, then its "run" record (left out when its result can't be known). Other
    tools give one record, or none for tools we don't track."""
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    now = time.time()
    failed = payload.get("hook_event_name") == "PostToolUseFailure"

    if tool in SHELL_TOOLS:
        if result_unknown(payload):
            return [], ""   # neither a pass nor a fail (and we can't know whether it wrote anything yet)
        cmd, exit_ok, output = bash_result(payload)
        if not cmd.strip():
            return [], ""
        edits = [{"kind": "edit", "t": now, "path": path, "via": "shell"} for path in shell_edits(cmd)]
        ok = check_result(cmd, exit_ok, output)
        if ok is None:
            return edits, output   # the check's result isn't in the output: record no pass and no fail
        return edits + [run_record(cmd, ok, output, now)], output
    if tool in EDIT_TOOLS and not failed:   # a failed edit changed nothing
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return [{"kind": "edit", "t": now, "path": str(path)}], ""
    if tool in READ_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or ""
        return [{"kind": "read", "t": now, "path": str(path)}], ""
    return [], ""


def action_from_tool_use(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The main history record for a tool call (a shell command's run, else its last edit), or None."""
    actions, output = actions_from_tool_use(payload)
    return (actions[-1] if actions else None), output


def tool_response_text(payload: dict[str, Any]) -> str:
    return _text(payload.get("tool_response"))
