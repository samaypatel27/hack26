"""rewind.py: roll the project's files back to a checkpoint, after the user said yes (docs/rewind.md).

The hook never rewinds by itself. It sends REWIND_OFFER with the exact command; the agent asks the
user and, on a yes, runs it through its Bash tool (so Claude Code's permission prompt is a second
confirmation). This script then:
  1. finds the checkpoint (a name the hook's notes know, or a hidden git ref of this session);
  2. restores it with checkpoints.restore(): saves "before_rewind" first, rewrites the changed files,
     deletes files created after the checkpoint. Files only; never the branch, index or history;
  3. updates the session's notes: a "rewind" record in the history (struggles.py counts attempts only
     after it, so the signals don't fire again on attempts made to files that no longer exist), and
     the open episode is marked rewound (no second offer; the help ladder carries on where it was);
  4. prints the CLEAN SLATE note: where it went back to, what changed, what already failed, how to undo.

Usage:
  python rewind.py --project DIR --session ID last_good        the last time the goal command passed
  python rewind.py --project DIR --session ID episode_start    when this problem started
  python rewind.py --project DIR --session ID before_rewind    undo the last rewind
  python rewind.py --project DIR --session ID 7                a checkpoint id (see --list)
  python rewind.py --project DIR --session ID --list

Exit code 0 on success, 1 on failure (with a message on stderr; files are left as they were, or the
message says how to get them back).
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checkpoints  # noqa: E402
import messages  # noqa: E402
import notebook  # noqa: E402

NAMES = ("last_good", "episode_start", "before_rewind")
TRIED_LIMIT = 8   # same as brain.TRIED_LIMIT


def _title(entry: dict[str, Any]) -> str:
    """Plain words for a checkpoint, for the note."""
    label, cmd = entry.get("label", ""), entry.get("cmd") or entry.get("key") or ""
    if label == "last_good":
        return f"the last time `{cmd}` passed" if cmd else "the last time the goal command passed"
    if label == "episode_start":
        return f"when this problem started (the first failure of `{cmd}`)" if cmd else "when this problem started"
    if label == "before_rewind":
        return "just before the last rewind"
    return f"checkpoint #{entry.get('id')} ({label})"


def resolve(project: Path, session: str, state: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The checkpoint `name` means: the hook's notes first, then this session's git refs. A name like
    episode_start falls back to git labels only when the notes are missing, because the notes know which
    episode_start belongs to the open episode."""
    entry = notebook.checkpoint_for(state, name)
    if entry:
        return entry
    listed = checkpoints.list_checkpoints(project, session, checkpoints.RESTORE_TIMEOUT)
    items = listed.get("checkpoints") or []
    if name.isdigit():
        return next((c for c in items if c["id"] == int(name)), None)
    if name.startswith(checkpoints.session_prefixes(session)):
        return next((c for c in items if c["ref"] == name), None)
    if state.get("checkpoints"):
        return None
    return next((c for c in reversed(items) if c["label"] == name), None)


def record_rewind(state: dict[str, Any], name: str, entry: dict[str, Any], result: dict[str, Any]) -> None:
    """Tell the hook (through the session's notes) that the files were rewound."""
    if result.get("before_rewind", {}).get("ok"):
        notebook.add_checkpoint(state, result["before_rewind"], "before_rewind", len(state["history"]) - 1)
    notebook.record(state, {"kind": notebook.REWIND_KIND, "t": time.time(), "to": name, "ref": entry["ref"]})
    episode = state.get("episode")
    if episode:
        episode["rewound"] = True
        episode["rewind_offered"] = True
        episode["last_nudge_idx"] = len(state["history"])   # the usual pause before the next ladder step
        note = "undid the rewind" if name == "before_rewind" else f"rewound the files to {name}"
        episode["tried"] = (episode.get("tried", []) + [note])[-TRIED_LIMIT:]


def show_list(project: Path, session: str, state: dict[str, Any]) -> int:
    listed = checkpoints.list_checkpoints(project, session, checkpoints.RESTORE_TIMEOUT)
    if not listed.get("ok"):
        print(f"Cannot list checkpoints: {listed.get('reason')}", file=sys.stderr)
        return 1
    items = listed["checkpoints"]
    if not items:
        print(f"No checkpoints for session {session} in {project}.")
        return 0
    names = {n: (notebook.checkpoint_for(state, n) or {}).get("ref") for n in NAMES}
    print(f"Checkpoints for session {session} (oldest first):")
    for item in items:
        labels = sorted({e["label"] for e in state.get("checkpoints", []) if e.get("ref") == item["ref"]}
                        | {item["label"]})
        current = [n for n, ref in names.items() if ref == item["ref"]]
        mark = f"   <- {', '.join(current)}" if current else ""
        print(f"  #{item['id']:<3} {'/'.join(labels):<28} {messages.clock(item['time'])}{mark}")
    print(f"Rewind with: {messages.rewind_command(project, session, '<name or id>')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Roll the project's files back to a Breakpoint checkpoint.")
    parser.add_argument("--project", required=True, help="the project folder (the hook's message gives it)")
    parser.add_argument("--session", required=True, help="the Claude Code session id")
    parser.add_argument("checkpoint", nargs="?", help="last_good | episode_start | before_rewind | <id>")
    parser.add_argument("--list", action="store_true", help="list this session's checkpoints")
    args = parser.parse_args(argv)
    if not args.list and not args.checkpoint:
        parser.error("give a checkpoint (last_good, episode_start, before_rewind or an id), or --list")

    project = Path(args.project).expanduser().resolve()
    os.environ["CLAUDE_PROJECT_DIR"] = str(project)   # the notebook then finds this session's notes file
    payload = {"session_id": args.session, "cwd": str(project)}
    state = notebook.load(payload)
    if args.list:
        return show_list(project, args.session, state)

    entry = resolve(project, args.session, state, args.checkpoint)
    if not entry:
        print(f"No checkpoint '{args.checkpoint}' for session {args.session} in {project}. "
              f"Nothing was changed. See what exists with --list.", file=sys.stderr)
        return 1

    tried = list((state.get("episode") or {}).get("tried") or [])
    result = checkpoints.restore(project, args.session, entry["ref"])
    if not result.get("ok"):
        if result.get("before_rewind", {}).get("ok"):   # keep the undo point findable by name
            notebook.add_checkpoint(state, result["before_rewind"], "before_rewind", len(state["history"]) - 1)
            notebook.save(payload, state)
        print(f"Rewind failed: {result.get('reason')}", file=sys.stderr)
        return 1

    record_rewind(state, args.checkpoint, entry, result)
    notebook.save(payload, state)
    command = (state.get("last_error") or {}).get("cmd") or (state.get("episode") or {}).get("key") or ""
    print(messages.clean_slate(args.checkpoint, _title(entry), messages.clock(entry["time"]), result, tried,
                               command, messages.rewind_command(project, args.session, "before_rewind")))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
