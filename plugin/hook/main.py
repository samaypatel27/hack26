"""main.py: the one script Claude Code runs for every hook event (plugin/hooks/hooks.json).

It reads the event JSON from stdin, loads this session's notebook, hands the event to the matching
handler in brain.py, saves the notebook, and prints at most one JSON object for Claude Code.

Rules (PROJECT_PLAN.md, "Rules for every hook"):
  - always exit 0, never exit 2: a broken hook must never block or crash the agent;
  - any error is written to <project>/.claude/loop_state/_errors.log and otherwise ignored;
  - specialist sessions (BREAKPOINT_SPECIALIST=1, or the old name AGENTOVERFLOW_SPECIALIST=1: the local
    tester's Claude sessions in plugin/tester/test_fixes.py) do nothing at all, so they can never trigger the hook and start a
    chain reaction of more help requests;
  - output is pure ASCII JSON (ensure_ascii), because Windows stdout is cp1252.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

HANDLER_NAMES = {
    "SessionStart": "on_session_start",
    "UserPromptSubmit": "on_user_prompt",
    "PostToolUse": "on_tool_use",
    "PostToolUseFailure": "on_tool_use",
    "Stop": "on_stop",
}


def _log_error(payload: dict[str, Any] | None) -> None:
    """Append the current traceback to _errors.log. Never raises."""
    try:
        cwd = (payload or {}).get("cwd") if isinstance(payload, dict) else None
        folder = Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd()) / ".claude" / "loop_state"
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / ".gitignore").exists():
            (folder / ".gitignore").write_text("*\n", encoding="utf-8")
        event = payload.get("hook_event_name") if isinstance(payload, dict) else None
        with open(folder / "_errors.log", "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {event}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def run(raw: bytes) -> dict[str, Any] | None:
    """Handle one event; returns the JSON to print, or None."""
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        return None
    name = HANDLER_NAMES.get(payload.get("hook_event_name", ""))
    if not name:
        return None
    try:
        import backend
        import brain
        import notebook
        state = notebook.load(payload)
        output = getattr(brain, name)(payload, state, backend)
        notebook.save(payload, state)
        return output
    except Exception:
        _log_error(payload)
        return None


def main() -> None:
    if "1" in (os.environ.get("BREAKPOINT_SPECIALIST"), os.environ.get("AGENTOVERFLOW_SPECIALIST")):
        return
    try:
        output = run(sys.stdin.buffer.read())
        if output:
            sys.stdout.write(json.dumps(output) + "\n")
            sys.stdout.flush()
    except Exception:
        _log_error(None)


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
