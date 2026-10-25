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

This file also turns raw hook payloads into history records (action_from_tool_use).
"""

import hashlib
import json
import os
import re
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
CHECKPOINT_LIMIT = 20   # entries kept in the notes (checkpoints.py keeps the same number of git refs)
REWIND_KIND = "rewind"  # history record written by rewind.py; struggles.py counts from the last one

EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}

# `pytest | tail` exits 0 even when pytest failed, so a piped command's output is checked too.
PIPED_FAILURE_RE = re.compile(
    r"\b[1-9]\d* failed\b|^FAILED|Traceback \(most recent call last\)|^npm ERR!|^Error:",
    re.MULTILINE,
)

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
    }


def load(payload: dict[str, Any]) -> dict[str, Any]:
    """This session's state, or a fresh one if there is none yet (or the file is unreadable)."""
    try:
        state = json.loads(state_path(payload).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict) or not isinstance(state.get("history"), list):
        return new_state(payload)
    for key in ("episode", "last_judge", "last_error", "goal_key"):
        state.setdefault(key, None)
    if not isinstance(state.get("checkpoints"), list):
        state["checkpoints"] = []
    if not state.get("project_id"):
        state["project_id"] = project_id(project_dir(payload))
    return state


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
        if episode.get("last_nudge_idx") is not None:
            episode["last_nudge_idx"] -= extra
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
        "err_fp": None if ok else struggles.fingerprint(output),
        "out_fp": struggles.fingerprint(output, errors_only=False),
        "tail": _tail(output),
    }


def looks_failed(cmd: str, output: str) -> bool:
    """A piped command can exit 0 while the command before the pipe failed."""
    return "|" in cmd and bool(PIPED_FAILURE_RE.search(output))


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


def bash_result(payload: dict[str, Any]) -> tuple[str, bool, str]:
    """(command, passed?, output) for a Bash PostToolUse or PostToolUseFailure payload."""
    tool_input = payload.get("tool_input") or {}
    cmd = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
    if payload.get("hook_event_name") == "PostToolUseFailure":
        return cmd, False, _text(payload.get("error"))     # "Exit code 1\n<output>"

    response = payload.get("tool_response")
    if isinstance(response, dict):
        parts = [_text(response.get(k)) for k in ("stdout", "stderr", "output")]
        output = "\n".join(p for p in parts if p)
        code = next((response[k] for k in ("exit_code", "exitCode", "returncode") if k in response), 0)
        ok = not code
    else:
        output, ok = _text(response), True
    if ok and looks_failed(cmd, output):
        ok = False
    return cmd, ok, output


def action_from_tool_use(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The history record for a tool call (or None for tools we don't track), and the run output."""
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    now = time.time()
    failed = payload.get("hook_event_name") == "PostToolUseFailure"

    if tool == "Bash":
        cmd, ok, output = bash_result(payload)
        return (run_record(cmd, ok, output, now), output) if cmd.strip() else (None, "")
    if tool in EDIT_TOOLS and not failed:   # a failed edit changed nothing
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return {"kind": "edit", "t": now, "path": str(path)}, ""
    if tool in READ_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or ""
        return {"kind": "read", "t": now, "path": str(path)}, ""
    return None, ""


def tool_response_text(payload: dict[str, Any]) -> str:
    return _text(payload.get("tool_response"))
