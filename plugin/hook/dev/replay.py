"""Replay a scenario through the real hook, one event at a time, exactly as Claude Code would.

A scenario is a .jsonl file with one hook payload per line (see plugin/hook/dev/scenarios/). Each line
is piped into `python plugin/hook/main.py` as its own process, with the same session_id, cwd and
transcript_path filled in, and the hook's output is printed under it.

Special lines (not sent to the hook):
  {"_comment": "..."}     printed as a heading
  {"_assistant": "..."}   appended to the fake transcript as the agent's reply (the Stop hook reads it)
  {"_files": {"path": "text"}}   write files into the project (what an Edit event really changed)
  {"_git": ["init", "-q"]}       run git in the project, with a throwaway identity
  {"_rewind": "last_good"}       run rewind.py for this session, as the agent does after the user says yes
  {"_env": {"NAME": "value"}}    set environment variables for the lines after it (hook and _run)
  {"_run": "command"}            run a command in the project with bash, like the agent's Bash tool, and send
                                 its result to the hook as a Bash PostToolUse / PostToolUseFailure event
  In _env values and _run commands, {project}, {session}, {tester}, {dev} and {python} are filled in
  ({tester} = plugin/tester/test_fixes.py, {dev} = this folder).

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
REWIND = Path(__file__).resolve().parents[1] / "rewind.py"
TESTER = Path(__file__).resolve().parents[2] / "tester" / "test_fixes.py"
DEV = Path(__file__).resolve().parent
GIT = ["git", "-c", "user.email=replay@example.com", "-c", "user.name=replay", "-c", "commit.gpgsign=false"]


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
        if "_files" in payload:
            for rel, text in payload["_files"].items():
                (project / rel).parent.mkdir(parents=True, exist_ok=True)
                (project / rel).write_text(text, encoding="utf-8")
            continue
        if "_git" in payload:
            subprocess.run(GIT + payload["_git"], cwd=project, capture_output=True)
            continue
        if "_rewind" in payload:
            result = subprocess.run([sys.executable, str(REWIND), "--project", str(project), "--session",
                                     args.session, payload["_rewind"]], capture_output=True, env=env)
            text = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
            print(f"\n(agent runs rewind.py {payload['_rewind']}, exit {result.returncode})")
            print("    " + text.replace("\n", "\n    "))
            continue
        fill = {"project": project.as_posix(), "session": args.session, "tester": TESTER.as_posix(),
                "dev": DEV.as_posix(), "python": Path(sys.executable).as_posix()}
        if "_env" in payload:
            env.update({k: str(v).format(**fill) for k, v in payload["_env"].items()})
            continue
        if "_run" in payload:
            command = payload["_run"].format(**fill)
            started = time.time()
            sys.path.insert(0, str(TESTER.parent))
            from test_fixes import bash_path   # Git Bash on Windows, never WSL's bash.exe
            result = subprocess.run([bash_path() or "bash", "-c", command], cwd=project, capture_output=True, env=env)
            output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            print(f"\n(agent runs `{command[:100]}`, exit {result.returncode}, {time.time() - started:.1f}s)")
            print("    " + output.strip().replace("\n", "\n    "))
            if result.returncode == 0:
                payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command},
                           "tool_response": {"stdout": output, "stderr": "", "interrupted": False}}
            else:
                payload = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                           "tool_input": {"command": command}, "error": f"Exit code {result.returncode}\n{output}"}

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
        # git's object files are read-only on Windows: make them writable, then delete
        try:
            shutil.rmtree(project, onexc=lambda func, path, _: (os.chmod(path, 0o666), func(path)))
        except OSError:
            pass
    else:
        print(f"\nProject folder: {project}")


if __name__ == "__main__":
    main()
