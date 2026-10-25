"""Replay a scenario through the real hook, one event at a time, exactly as Claude Code would.

A scenario is a .jsonl file with one hook payload per line (see plugin/hook/dev/scenarios/). Each line
is piped into `python plugin/hook/main.py` as its own process, with the same session_id, cwd and
transcript_path filled in, and the hook's output is printed under it.

Special lines (not sent to the hook):
  {"_comment": "..."}     printed as a heading
  {"_assistant": "..."}   appended to the fake transcript as the agent's reply (the Stop hook reads it)

Usage (from the repo root):
  python plugin/hook/dev/replay.py plugin/hook/dev/scenarios/stuck_known_fix.jsonl
  python plugin/hook/dev/replay.py SCENARIO --api-url http://127.0.0.1:8000 --team-mode local
  python plugin/hook/dev/replay.py SCENARIO --project path/to/project --keep

Start the mock server first (python plugin/hook/dev/mock_backend.py) to see forum answers; without
a server the hook falls through to triage and the team, as it would with our API down.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "main.py"


def describe(payload: dict) -> str:
    event = payload.get("hook_event_name", "?")
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    detail = tool_input.get("command") or tool_input.get("file_path") or payload.get("prompt") or ""
    return " ".join(str(p) for p in (event, tool, detail[:80] if detail else "") if p)


def show(output: str) -> str:
    """The text the agent would see, or the raw output if it is not one of our shapes."""
    try:
        data = json.loads(output)
    except ValueError:
        return output
    if isinstance(data, dict) and data.get("decision") == "block":
        return "BLOCK STOP:\n" + data.get("reason", "")
    context = (data.get("hookSpecificOutput") or {}).get("additionalContext") if isinstance(data, dict) else None
    return context if context else output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--project", type=Path, help="project folder (default: a fresh temp folder)")
    parser.add_argument("--session", default=f"replay-{int(time.time())}")
    parser.add_argument("--api-url", help="backend URL (default: the hook's normal config)")
    parser.add_argument("--team-mode", choices=["server", "local"])
    parser.add_argument("--keep", action="store_true", help="keep the temp project folder")
    args = parser.parse_args()

    project = args.project or Path(tempfile.mkdtemp(prefix="agentoverflow_replay_"))
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    env.pop("AGENTOVERFLOW_SPECIALIST", None)
    if args.api_url:
        env["AGENTOVERFLOW_API_URL"] = args.api_url
    if args.team_mode:
        env["AGENTOVERFLOW_TEAM_MODE"] = args.team_mode

    step = 0
    for line in args.scenario.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if "_comment" in payload:
            print(f"\n## {payload['_comment']}")
            continue
        if "_assistant" in payload:
            entry = {"type": "assistant", "message": {"role": "assistant",
                                                      "content": [{"type": "text", "text": payload["_assistant"]}]}}
            with open(transcript, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            print(f"\n(agent says) {payload['_assistant']}")
            continue

        step += 1
        payload.setdefault("session_id", args.session)
        payload.setdefault("cwd", str(project))
        payload.setdefault("transcript_path", str(transcript))
        started = time.time()
        result = subprocess.run([sys.executable, str(MAIN)], input=json.dumps(payload).encode("utf-8"),
                                capture_output=True, env=env)
        ms = int((time.time() - started) * 1000)
        print(f"\n[{step}] {describe(payload)}  ({ms} ms, exit {result.returncode})")
        out = result.stdout.decode("utf-8", errors="replace").strip()
        if out:
            print("    " + show(out).replace("\n", "\n    "))
        if result.stderr:
            print("    stderr: " + result.stderr.decode("utf-8", errors="replace").strip())

    errors = project / ".claude" / "loop_state" / "_errors.log"
    if errors.exists():
        print(f"\nHook errors were logged: {errors}")
    if args.project is None and not args.keep:
        shutil.rmtree(project, ignore_errors=True)
    else:
        print(f"\nProject folder: {project}")


if __name__ == "__main__":
    main()
