"""test_fixes.py: the local tester. Tries the backup team's ranked fixes in throwaway copies of the project
(docs/local-tester.md, PROJECT_PLAN.md "The backup team").

The hook never runs this by itself. When the team's report arrives it sends TEST_FIXES with the exact
command; the agent asks the user and, on a yes, runs it through its Bash tool (so Claude Code's permission
prompt is a second yes). Nothing in the real project is ever changed: every hypothesis gets its own copy, and
the agent applies the winning change itself, where the user can see it.

For each hypothesis (top --max-hypotheses, in parallel):
  1. copy: `git worktree add --detach <system temp>/... <commit>`, where <commit> is the session's
     "episode_start" checkpoint (checkpoints.py; it includes uncommitted and new files), else a fresh
     checkpoint, else HEAD. Heavy ignored folders (node_modules, .venv, venv) are linked in (a Windows
     junction or a symlink), or copied when the hypothesis has to install something, or skipped (--deps);
  2. session: headless Claude Code (`claude -p`, Sonnet, a turn limit, acceptEdits, no permission prompts,
     no MCP servers) applies ONLY that hypothesis's steps in the copy, with AGENTOVERFLOW_SPECIALIST=1 so our
     hook stays silent there and can never start a chain reaction;
  3. check: this script reruns the check command in the copy itself (the session's claim is never trusted)
     and captures `git diff` of the copy against the starting commit (new files included);
  4. cleanup: links removed, `git worktree remove --force`, `git worktree prune`. Always, even on errors.
An untouched copy (the baseline) runs the check too: if it already passes there, the copy cannot prove a fix.

With no check command anywhere, a first session writes a small test that reproduces the symptom; it must
fail in an untouched copy, and then it is the check for every hypothesis (and is left out of their diffs).

Output: the first line is `WINNER: hypothesis <n> (<cause>)`, `NO_WINNER: <n> hypotheses tried` or
`INCONCLUSIVE: <reason>` (the hook reads it), then per-hypothesis results, the winner's full diff and per-stage
timing. The full result is also saved to <project>/.claude/loop_state/tester_<session>.json.

Usage:
  python test_fixes.py --project DIR --session ID --run-id RUN [--check "CMD"]
  python test_fixes.py --project DIR --session ID --report report.json [--check "CMD"]
  options: --max-hypotheses 3  --timeout 180  --check-timeout 120  --parallel 3 | --no-parallel
           --deps link|copy|none  --setup "CMD"  --max-turns 30  --model sonnet  --keep

Tests (and demos) replace `claude` with a fake: env AGENTOVERFLOW_TESTER_CLAUDE = a JSON list or a command line.
Standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HOOK_DIR = Path(__file__).resolve().parents[1] / "hook"
sys.path.insert(0, str(HOOK_DIR))

import backend  # noqa: E402
import brain  # noqa: E402
import checkpoints  # noqa: E402
import notebook  # noqa: E402

CLAUDE_ENV = "AGENTOVERFLOW_TESTER_CLAUDE"   # override the claude command (tests use a fake)
MODEL = "sonnet"
MAX_TURNS = 30
SESSION_TIMEOUT = 180       # seconds per hypothesis session
CHECK_TIMEOUT = 120         # seconds per check run
SETUP_TIMEOUT = 300
MAX_HYPOTHESES = 3
MAX_PARALLEL = 3
FETCH_TIMEOUT = 15.0
GIT_TIMEOUT = 120.0
TAIL_LINES = 25
TAIL_CHARS = 3000
DIFF_CHARS = 60_000         # diff kept per hypothesis in the result file
SESSION_TEXT_CHARS = 1500
COPY_PREFIX = "agentoverflow_tester_"
REPRO_LABEL = "tester_start"
STEPS_SHOWN = 12
LINK_DIRS = ("node_modules", ".venv", "venv")

# Commands a hypothesis's steps may run in its copy: exact backticked commands from the steps, only for package
# managers that install into the project folder (never pip/conda: those usually change a shared Python), and
# never with shell operators. Everything else the session wants to run is denied without a prompt.
ALLOWED_COMMAND_RE = re.compile(r"^(npm|npx|pnpm|yarn|bun|cargo|go|dotnet|bundle|composer)\s+[\w@./:=^~+-][^|;&<>$`\n]*$")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
CHECK_LINE_RE = re.compile(r"^\s*CHECK:\s*(.+?)\s*$", re.MULTILINE)

# Session-specific variables of the Claude Code that runs this script: a nested session must not inherit them.
PARENT_SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_EMIT_STARTUP_TIMING",
    "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "GIT_INDEX_FILE", "GIT_DIR",
    "GIT_WORK_TREE",
)

IS_WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

_git_lock = threading.Lock()       # git worktree add/remove, one at a time
_live_copies: dict[str, "Copy"] = {}
_live_lock = threading.Lock()


class TesterError(Exception):
    """Something that stops the whole run (no report, not a git repo, ...). Printed as INCONCLUSIVE."""


# ---------------------------------------------------------------------------
# Running processes (with a timeout that kills the whole process tree)
# ---------------------------------------------------------------------------

def run_proc(args: list[str] | str, cwd: Path, env: dict[str, str] | None = None, timeout: float = 60,
             stdin: str | None = None, shell: bool = False) -> dict[str, Any]:
    """{"exit", "out" (stdout+stderr), "stdout", "timed_out", "seconds"}. Never raises for the command itself."""
    started = time.time()
    kwargs: dict[str, Any] = {"cwd": str(cwd), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                              "stdin": subprocess.PIPE if stdin is not None else subprocess.DEVNULL, "shell": shell}
    if IS_WINDOWS:
        kwargs["creationflags"] = NO_WINDOW | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kwargs)
    except OSError as exc:
        return {"exit": None, "out": f"could not start: {exc}", "stdout": "", "timed_out": False,
                "seconds": round(time.time() - started, 2)}
    timed_out = False
    try:
        out, err = proc.communicate(stdin.encode("utf-8") if stdin is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out, err = b"", b""
    stdout = (out or b"").decode("utf-8", errors="replace")
    stderr = (err or b"").decode("utf-8", errors="replace")
    return {"exit": proc.returncode, "out": "\n".join(p for p in (stdout, stderr) if p.strip()), "stdout": stdout,
            "timed_out": timed_out, "seconds": round(time.time() - started, 2)}


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=20,
                           creationflags=NO_WINDOW)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def git(cwd: Path, *args: str, timeout: float = GIT_TIMEOUT, check: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")}
    env.update(checkpoints.IDENTITY, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, timeout=timeout,
                                creationflags=NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError) as exc:
        if check:
            raise TesterError(f"`git {args[0]}` failed: {exc}") from exc
        return ""
    if check and result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise TesterError(f"`git {' '.join(args[:2])}` failed: {err[-1] if err else result.returncode}")
    return result.stdout.decode("utf-8", errors="replace")


def bash_path() -> str | None:
    """Git Bash on Windows (the shell Claude Code's Bash tool uses; never WSL's bash.exe), bash elsewhere."""
    if not IS_WINDOWS:
        return shutil.which("bash")
    configured = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("git")
    if found:
        for root in Path(found).resolve().parents[:3]:
            for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return None


def run_shell(command: str, cwd: Path, env: dict[str, str], timeout: float) -> dict[str, Any]:
    """A check or setup command, the way the agent's Bash tool would run it."""
    bash = bash_path()
    if bash:
        return run_proc([bash, "-c", command], cwd, env, timeout)
    return run_proc(command, cwd, env, timeout, shell=True)


def claude_command() -> list[str]:
    """The claude CLI (the native exe behind npm's claude.cmd when we can find it), or the test override."""
    override = os.environ.get(CLAUDE_ENV, "").strip()
    if override:
        if override.startswith("["):
            return [str(p) for p in json.loads(override)]
        return shlex.split(override, posix=not IS_WINDOWS)
    found = shutil.which("claude")
    if not found:
        raise TesterError("the `claude` command was not found on PATH")
    if IS_WINDOWS and found.lower().endswith(".cmd"):
        try:
            match = re.search(r'"%dp0%\\([^"]+\.exe)"', Path(found).read_text(encoding="utf-8", errors="replace"))
            if match and (Path(found).parent / match.group(1)).is_file():
                return [str(Path(found).parent / match.group(1))]
        except OSError:
            pass
    return [found]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
    env["AGENTOVERFLOW_SPECIALIST"] = "1"   # our hook stays silent in tester sessions (main.py)
    env.update(extra or {})
    return env


def tail(text: str, lines: int = TAIL_LINES, chars: int = TAIL_CHARS) -> str:
    kept = "\n".join((text or "").rstrip().splitlines()[-lines:])
    return kept if len(kept) <= chars else "..." + kept[-chars:]


# ---------------------------------------------------------------------------
# The team's report and the check command
# ---------------------------------------------------------------------------

def load_report(run_id: str | None, report_file: str | None) -> dict[str, Any]:
    """The report dict ({"hypotheses": [...], ...}) from a file or from GET /v1/help/{run_id}."""
    if report_file:
        try:
            data = json.loads(Path(report_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TesterError(f"cannot read the report file: {exc}") from exc
    else:
        data = backend._request("GET", f"/v1/help/{run_id}", FETCH_TIMEOUT)
        if not isinstance(data, dict):
            raise TesterError(f"could not fetch team run {run_id} from {backend.api_url()}")
        status = data.get("status")
        if status and status != "report":
            raise TesterError(f"team run {run_id} has no report yet (status: {status}); call check_help later")
    report = data.get("report") if isinstance(data.get("report"), dict) else data
    if not isinstance(report.get("hypotheses"), list) or not report["hypotheses"]:
        raise TesterError("the team's report has no hypotheses to test")
    report.setdefault("case", data.get("case") if isinstance(data.get("case"), dict) else {})
    return report


def hypotheses_from(report: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    items = [h for h in report["hypotheses"] if isinstance(h, dict)]
    items.sort(key=lambda h: (h.get("rank") if isinstance(h.get("rank"), (int, float)) else 99))
    picked = []
    for n, h in enumerate(items[:limit], 1):
        steps = h.get("steps") or []
        steps = [str(s) for s in (steps if isinstance(steps, list) else [steps]) if str(s).strip()]
        picked.append({"n": n, "rank": h.get("rank", n), "cause": str(h.get("cause") or f"hypothesis {n}"),
                       "confidence": h.get("confidence"), "steps": steps, "evidence": h.get("evidence") or [],
                       "check": h.get("check") if isinstance(h.get("check"), dict) else {}})
    return picked


def pick_check(given: str | None, report: dict[str, Any], hypotheses: list[dict[str, Any]],
               state: dict[str, Any]) -> tuple[str | None, str]:
    """(check command, where it came from). One check for every hypothesis, so they are compared fairly."""
    if given and given.strip():
        return given.strip(), "--check"
    case = report.get("case") or {}
    for source, value in (("report", report.get("check_command")), ("case file", case.get("check_command"))):
        if isinstance(value, str) and value.strip():
            return value.strip(), source
    commands = [h["check"].get("command") for h in hypotheses if isinstance(h["check"].get("command"), str)]
    commands = [c.strip() for c in commands if c and c.strip()]
    if commands:
        return max(set(commands), key=commands.count), "hypotheses"
    last = state.get("last_error") or {}
    if last.get("cmd"):
        return str(last["cmd"]), "the session's last failing command"
    return None, ""


def symptom_text(report: dict[str, Any], hypotheses: list[dict[str, Any]], state: dict[str, Any]) -> str:
    """What is wrong, for the reproduce-it session: the case file, the team's checks, the session's last error."""
    case = report.get("case") or {}
    parts = [f"{k}: {case[k]}" for k in ("goal", "symptom", "expected", "actual", "check_description")
             if case.get(k)]
    for key in ("symptom", "goal"):
        if report.get(key):
            parts.append(f"{key}: {report[key]}")
    descriptions = [h["check"].get("description") for h in hypotheses if h["check"].get("description")]
    if descriptions:
        parts.append("how the team says a fix can be recognized: " + "; ".join(dict.fromkeys(descriptions)))
    last = state.get("last_error") or {}
    if last.get("text"):
        parts.append(f"last failing command: {last.get('cmd', '')}\nits output:\n{tail(last['text'], 30)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Where the copies start: the episode_start checkpoint
# ---------------------------------------------------------------------------

def start_commit(project: Path, top: Path, session: str, state: dict[str, Any]) -> tuple[str, str]:
    """(commit, description): episode_start, else a fresh checkpoint of the files now, else HEAD."""
    entry = notebook.checkpoint_for(state, "episode_start")
    if entry and entry.get("ref"):
        commit = git(top, "rev-parse", "-q", "--verify", f"{entry['ref']}^{{commit}}", check=False).strip()
        if commit:
            return commit, f"checkpoint episode_start ({entry['ref']}): the files when this problem started"
    saved = checkpoints.save(project, session, REPRO_LABEL, checkpoints.RESTORE_TIMEOUT)
    if saved.get("ok"):
        return saved["commit"], f"a fresh checkpoint of the files now ({saved['ref']}); no episode_start was found"
    commit = git(top, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).strip()
    if commit:
        return commit, "HEAD (the last commit; uncommitted changes are NOT in the copies)"
    raise TesterError(f"cannot find a starting point for the copies: {saved.get('reason')}")


# ---------------------------------------------------------------------------
# Copies (git worktrees outside the project) and their dependency folders
# ---------------------------------------------------------------------------

class Copy:
    """One throwaway worktree in the system temp folder. Always call remove()."""

    def __init__(self, top: Path, rel: Path, commit: str, name: str):
        self.top, self.commit, self.name = top, commit, name
        self.parent = Path(tempfile.mkdtemp(prefix=f"{COPY_PREFIX}{name}_"))
        self.root = self.parent / "copy"
        self.workdir = self.root / rel
        self.links: list[Path] = []
        self.copied: list[str] = []
        self.removed = False
        self.problem = ""
        empty_hooks = self.parent / "no-hooks"     # the user's git hooks (post-checkout, ...) never run here
        empty_hooks.mkdir()
        with _git_lock:
            git(top, "-c", f"core.hooksPath={empty_hooks.as_posix()}", "worktree", "add", "--detach", "-f",
                str(self.root), commit)
        with _live_lock:
            _live_copies[str(self.root)] = self

    def add_dependencies(self, project: Path, mode: str, names: list[str]) -> list[str]:
        """Link (or copy) heavy folders that git ignores, so the check can run without a fresh install."""
        notes = []
        if mode == "none":
            return notes
        for name in names:
            src, dst = project / name, self.workdir / name
            if not src.is_dir() or dst.exists() or os.path.lexists(dst):
                continue
            if not git_ignores(project, name):
                continue   # tracked folders are already in the copy; never link something git would add
            try:
                if mode == "copy":
                    shutil.copytree(src, dst, symlinks=True)
                    self.copied.append(name)
                    notes.append(f"{name} (private copy)")
                else:
                    make_link(src, dst)
                    self.links.append(dst)
                    notes.append(f"{name} (shared link, read-only by rule)")
            except OSError as exc:
                notes.append(f"{name} (not available: {exc})")
        return notes

    def remove(self) -> None:
        """Remove the links first (so nothing can delete through them), then the worktree, then the folder."""
        if self.removed:
            return
        for link in self.links:
            try:
                remove_link(link)
            except OSError as exc:
                self.problem = f"could not remove the link {link}: {exc}"
        if any(os.path.lexists(link) for link in self.links):
            self.problem = self.problem or "a dependency link is still there"
            return   # never delete a folder that may still point into the real project
        with _git_lock:
            for attempt in range(3):
                result = subprocess.run(["git", "worktree", "remove", "--force", "--force", str(self.root)],
                                        cwd=str(self.top), capture_output=True, creationflags=NO_WINDOW)
                if result.returncode == 0 or not self.root.exists():
                    break
                time.sleep(1 + attempt)   # Windows: a just-killed process may still hold a file
        shutil.rmtree(self.parent, onexc=_force_remove)
        if self.parent.exists():
            self.problem = f"could not delete {self.parent}"
        self.removed = True
        with _live_lock:
            _live_copies.pop(str(self.root), None)


def _force_remove(func, path, _exc):
    """Windows: git object files are read-only, so make them writable before deleting."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def git_ignores(project: Path, name: str) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", name], cwd=str(project), capture_output=True,
                            creationflags=NO_WINDOW)
    return result.returncode == 0


def make_link(src: Path, dst: Path) -> None:
    """A directory junction on Windows (no admin rights needed), a symlink elsewhere."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        import _winapi
        _winapi.CreateJunction(str(src.resolve()), str(dst))
    else:
        os.symlink(src.resolve(), dst, target_is_directory=True)


def remove_link(link: Path) -> None:
    """Remove only the link itself, never what it points to."""
    if not os.path.lexists(link):
        return
    if IS_WINDOWS and (link.is_junction() or link.is_symlink()):
        os.rmdir(link)
    elif link.is_symlink():
        os.unlink(link)
    else:
        raise OSError("not a link")


def cleanup_all() -> None:
    with _live_lock:
        copies = list(_live_copies.values())
    for copy in copies:
        try:
            copy.remove()
        except Exception:
            pass


def change_set(copy: Copy, exclude: list[str]) -> dict[str, Any]:
    """The copy's changes against the starting commit, new files included. Uses the copy's own index only."""
    spec = ["--", ".", ":(exclude).claude", *[f":(exclude){p}" for p in exclude]]
    git(copy.root, "add", "-A", *spec)
    names = git(copy.root, "diff", "--cached", "--name-status", "--no-renames", copy.commit, *spec)
    stat_line = git(copy.root, "diff", "--cached", "--shortstat", copy.commit, *spec).strip()
    diff = git(copy.root, "diff", "--cached", "--no-color", copy.commit, *spec)
    files = [line.split("\t", 1)[1] for line in names.splitlines() if "\t" in line]
    return {"files": files, "stat": stat_line or "no changes", "diff": diff}


def file_hashes(root: Path, paths: list[str]) -> dict[str, str]:
    hashes = {}
    for rel in paths:
        try:
            hashes[rel] = hashlib.sha1((root / rel).read_bytes()).hexdigest()
        except OSError:
            hashes[rel] = "missing"
    return hashes


# ---------------------------------------------------------------------------
# Headless Claude Code sessions
# ---------------------------------------------------------------------------

def allowed_commands(steps: list[str]) -> list[str]:
    commands = []
    for step in steps:
        for found in BACKTICK_RE.findall(step):
            found = found.strip()
            if ALLOWED_COMMAND_RE.match(found) and found not in commands:
                commands.append(found)
    return commands


def claude_args(model: str, max_turns: int, commands: list[str]) -> list[str]:
    """Flags for `claude -p` (checked against `claude --help`, Claude Code 2.1). The prompt goes in on stdin.
      --permission-mode acceptEdits   file edits inside the working directory (the copy) need no prompt;
                                      anything else would need one...
      --permission-prompts none       ...and nobody answers it, so it is denied at once (no hang)
      --allowedTools Bash(<cmd>)      except the exact package-manager commands the steps name (and PowerShell(<cmd>))
                                      (Claude Code itself still auto-allows read-only commands such as `node --version`)
      --disallowedTools WebFetch WebSearch   the team already did the research
      --strict-mcp-config             no MCP servers: a tester can never post to the forum or call the team
      --no-session-persistence        tester sessions don't clutter the user's /resume list
    """
    args = ["-p", "--model", model, "--max-turns", str(max_turns), "--output-format", "json",
            "--permission-mode", "acceptEdits", "--permission-prompts", "none", "--strict-mcp-config",
            "--no-session-persistence", "--disallowedTools", "WebFetch", "WebSearch"]
    if commands:   # on Windows the session may pick its PowerShell tool instead of Bash: allow both, exactly
        args += ["--allowedTools", *[f"{tool}({c})" for c in commands for tool in ("Bash", "PowerShell")]]
    return args


def run_session(workdir: Path, prompt: str, model: str, max_turns: int, timeout: float,
                commands: list[str]) -> dict[str, Any]:
    argv = claude_command() + claude_args(model, max_turns, commands)
    proc = run_proc(argv, workdir, child_env(), timeout, stdin=prompt)
    info: dict[str, Any] = {"exit": proc["exit"], "timed_out": proc["timed_out"], "seconds": proc["seconds"],
                            "turns": None, "cost_usd": None, "result": "", "error": None}
    data = None
    for line in reversed(proc["stdout"].strip().splitlines()):
        try:
            data = json.loads(line)
            break
        except ValueError:
            continue
    if isinstance(data, dict):
        info.update(turns=data.get("num_turns"), cost_usd=data.get("total_cost_usd"),
                    result=str(data.get("result") or "")[-SESSION_TEXT_CHARS:])
        if data.get("is_error") or str(data.get("subtype", "success")) != "success":
            info["error"] = str(data.get("subtype") or "error")
    else:
        info["result"] = tail(proc["out"], 15, SESSION_TEXT_CHARS)
    if proc["timed_out"]:
        info["error"] = f"timed out after {timeout:g}s"
    elif proc["exit"] not in (0, None) and not info["error"]:
        info["error"] = f"exit {proc['exit']}"
    elif proc["exit"] is None:
        info["error"] = proc["out"]
    return info


def hypothesis_prompt(h: dict[str, Any], workdir: Path, check: str, symptom: str, deps: list[str],
                      commands: list[str], protected: list[str]) -> str:
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(h["steps"][:STEPS_SHOWN], 1)) or "(no steps given)"
    evidence = [e.get("url") for e in h["evidence"] if isinstance(e, dict) and e.get("url")][:3]
    lines = [
        "You are the local tester for a team of research agents. This directory is a THROWAWAY COPY of a",
        f"project: {workdir}",
        "",
        "Your only job: apply the steps of the ONE hypothesis below to the files in this directory, then stop.",
        "Rules:",
        "- Work only inside this directory. Do not read, change or create anything outside it.",
        "- Apply ONLY this hypothesis's steps. Do not try other fixes, refactor, or improve anything else.",
        "- Never skip, delete or weaken tests, and never edit the check's files to make it pass.",
        "- Do not run the check yourself: the tester reruns it after you stop and decides pass or fail.",
    ]
    if protected:
        lines.append(f"- Do not touch these files (they are the check): {', '.join(protected)}")
    if commands:
        lines.append("- You may run only these commands, exactly as written, if a step needs them: "
                     + "; ".join(f"`{c}`" for c in commands))
    else:
        lines.append("- Do not run commands, install packages or use the network. If a step needs a command, "
                     "make every file change you can and say which step you could not do.")
    if deps:
        lines.append(f"- Dependency folders here: {', '.join(deps)}. Never modify or delete a shared link.")
    lines += [
        "- When done, reply with one short paragraph: what you changed, or why a step could not be applied.",
        "",
        f"How the fix will be checked: `{check}`" if check else "",
        f"The problem:\n{symptom}" if symptom else "",
        "",
        f"Hypothesis {h['n']} (rank {h['rank']}, confidence {h.get('confidence')}): {h['cause']}",
        "Steps:",
        steps,
    ]
    if evidence:
        lines.append("Evidence: " + " ".join(evidence))
    return "\n".join(line for line in lines if line is not None)


def repro_prompt(workdir: Path, symptom: str) -> str:
    return "\n".join([
        "You are working in a THROWAWAY COPY of a project, at:",
        str(workdir),
        "",
        "Write ONE small automated test that reproduces the problem below. It must FAIL on the code as it is now",
        "and pass once the problem is fixed. Use the project's existing test framework and layout if there is one.",
        "Rules: work only inside this directory; only create new files (do not change existing ones); do not fix",
        "the problem; do not run commands or install anything.",
        "The LAST line of your reply must be exactly `CHECK: <the shell command that runs only this test>`,",
        "run from this directory.",
        "",
        "The problem:",
        symptom,
    ])


# ---------------------------------------------------------------------------
# One hypothesis, the baseline, and the reproduce-it step
# ---------------------------------------------------------------------------

def try_hypothesis(h: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    commands = allowed_commands(h["steps"])
    result: dict[str, Any] = {**{k: h[k] for k in ("n", "rank", "cause", "confidence", "steps")},
                              "status": "error", "passed": False, "allowed_commands": commands,
                              "times": {}, "session": {}, "check": {}, "files": [], "stat": "", "diff": "",
                              "suspect": [], "deps": [], "note": ""}
    copy = None
    try:
        t = time.time()
        copy = Copy(ctx["top"], ctx["rel"], ctx["commit"], f"h{h['n']}")
        mode = ctx["deps"] if ctx["deps"] == "none" or not commands else "copy"   # installs need a private copy
        result["deps"] = copy.add_dependencies(ctx["project"], mode, ctx["link_dirs"])
        if ctx.get("repro_patch"):
            apply_patch(copy, ctx["repro_patch"])
        if ctx.get("setup"):
            setup = run_shell(ctx["setup"], copy.workdir, child_env(), SETUP_TIMEOUT)
            if setup["exit"] != 0:
                result["note"] = f"--setup failed (exit {setup['exit']}): {tail(setup['out'], 8)}"
        protected = ctx.get("repro_files") or []
        before = file_hashes(copy.workdir, protected)
        result["times"]["copy"] = round(time.time() - t, 2)

        if not h["steps"]:
            result.update(status="no_steps", note="the report gives no steps for this hypothesis")
            return result
        prompt = hypothesis_prompt(h, copy.workdir, ctx["check"], ctx["symptom"], result["deps"], commands, protected)
        t = time.time()
        result["session"] = run_session(copy.workdir, prompt, ctx["model"], ctx["max_turns"], ctx["timeout"], commands)
        result["times"]["session"] = round(time.time() - t, 2)

        # The change set is taken before the check runs, so files the check writes (caches, builds) stay out.
        changes = change_set(copy, ctx.get("repro_files_top") or [])
        result.update(files=changes["files"], stat=changes["stat"], diff=changes["diff"][:DIFF_CHARS],
                      diff_truncated=len(changes["diff"]) > DIFF_CHARS)
        result["suspect"] = brain.suspect_changes(copy.root)
        if protected and file_hashes(copy.workdir, protected) != before:
            result["suspect"].append("changed the reproduce-it test that is used as the check")

        t = time.time()
        check = run_shell(ctx["check"], copy.workdir, child_env(), ctx["check_timeout"])
        result["times"]["check"] = round(time.time() - t, 2)
        result["check"] = {"command": ctx["check"], "exit": check["exit"], "timed_out": check["timed_out"],
                           "tail": tail(check["out"])}

        passed = check["exit"] == 0 and not check["timed_out"]
        if passed and not changes["files"]:
            result.update(status="no_changes", note="the check passed, but the session changed nothing")
        elif passed and result["suspect"]:
            result.update(status="suspect", note="the check passed, but the change hides the failure instead "
                                                 "of fixing it")
        elif passed:
            result.update(status="passed", passed=True)
        else:
            result["status"] = "failed"
        return result
    except TesterError as exc:
        result["note"] = str(exc)
        return result
    except Exception as exc:   # one broken hypothesis must not stop the others, or the cleanup
        result["note"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if copy and not ctx.get("keep"):
            t = time.time()
            copy.remove()
            result["times"]["cleanup"] = round(time.time() - t, 2)
            if copy.problem:
                result["note"] = (result["note"] + "; " if result["note"] else "") + copy.problem
        elif copy:
            result["note"] = (result["note"] + "; " if result["note"] else "") + f"kept: {copy.root}"
        result["times"]["total"] = round(time.time() - started, 2)


def run_baseline(ctx: dict[str, Any]) -> dict[str, Any]:
    """The check in an untouched copy. It should fail: otherwise the copy can't tell a fix from no fix."""
    started = time.time()
    copy = None
    try:
        copy = Copy(ctx["top"], ctx["rel"], ctx["commit"], "baseline")
        copy.add_dependencies(ctx["project"], ctx["deps"], ctx["link_dirs"])
        if ctx.get("repro_patch"):
            apply_patch(copy, ctx["repro_patch"])
        if ctx.get("setup"):
            run_shell(ctx["setup"], copy.workdir, child_env(), SETUP_TIMEOUT)
        check = run_shell(ctx["check"], copy.workdir, child_env(), ctx["check_timeout"])
        return {"passed": check["exit"] == 0 and not check["timed_out"], "exit": check["exit"],
                "timed_out": check["timed_out"], "tail": tail(check["out"], 12),
                "seconds": round(time.time() - started, 2)}
    except Exception as exc:
        return {"passed": None, "error": str(exc), "seconds": round(time.time() - started, 2)}
    finally:
        if copy:
            copy.remove()


def make_repro(ctx: dict[str, Any]) -> dict[str, Any]:
    """No check command: a session writes a failing test for the symptom; the test becomes the check."""
    started = time.time()
    copy = Copy(ctx["top"], ctx["rel"], ctx["commit"], "repro")
    try:
        session = run_session(copy.workdir, repro_prompt(copy.workdir, ctx["symptom"]), ctx["model"],
                              ctx["max_turns"], ctx["timeout"], [])
        found = CHECK_LINE_RE.findall(session.get("result") or "")
        if not found:
            raise TesterError("no check command was given, and the reproduce-it session did not return a "
                              f"`CHECK:` line ({session.get('error') or 'no error'})")
        check = found[-1].strip().strip("`")
        changes = change_set(copy, [])
        if not changes["files"]:
            raise TesterError("the reproduce-it session wrote no test file")
        patch = git(copy.root, "diff", "--cached", "--binary", copy.commit)
        rel_prefix = "" if str(ctx["rel"]) in ("", ".") else ctx["rel"].as_posix().rstrip("/") + "/"
        result = run_shell(check, copy.workdir, child_env(), ctx["check_timeout"])
        if result["exit"] == 0 and not result["timed_out"]:
            raise TesterError(f"the reproduce-it test `{check}` already passes, so it does not reproduce the problem")
        return {"check": check, "patch": patch, "files_top": changes["files"],
                "files": [f[len(rel_prefix):] if f.startswith(rel_prefix) else f for f in changes["files"]],
                "fails_with": tail(result["out"], 12), "session": session,
                "seconds": round(time.time() - started, 2)}
    finally:
        copy.remove()


def apply_patch(copy: Copy, patch: str) -> None:
    patch_file = copy.parent / "repro.patch"
    patch_file.write_text(patch, encoding="utf-8", newline="\n")
    git(copy.root, "apply", "--whitespace=nowarn", str(patch_file))


# ---------------------------------------------------------------------------
# The whole run
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.time()
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise TesterError(f"project folder not found: {project}")
    os.environ["CLAUDE_PROJECT_DIR"] = str(project)   # the notebook then finds this session's notes
    payload = {"session_id": args.session, "cwd": str(project)}
    state = notebook.load(payload)
    try:
        top = checkpoints.repo_root(project, GIT_TIMEOUT)
    except checkpoints.GitError as exc:
        raise TesterError(f"the project must be a git repository ({exc})") from exc
    rel = project.relative_to(top.resolve()) if project != top.resolve() else Path(".")
    report = load_report(args.run_id, args.report)
    hypotheses = hypotheses_from(report, max(1, args.max_hypotheses))
    git(top, "worktree", "prune", check=False)

    t = time.time()
    commit, source = start_commit(project, top, args.session, state)
    check, check_source = pick_check(args.check, report, hypotheses, state)
    ctx: dict[str, Any] = {
        "project": project, "top": top, "rel": rel, "commit": commit, "check": check,
        "symptom": symptom_text(report, hypotheses, state), "deps": args.deps, "setup": args.setup,
        "link_dirs": list(dict.fromkeys(list(LINK_DIRS) + (args.link or []))), "model": args.model,
        "max_turns": args.max_turns, "timeout": args.timeout, "check_timeout": args.check_timeout,
        "keep": args.keep,
    }
    result: dict[str, Any] = {"run_id": args.run_id, "session": args.session, "project": str(project),
                              "start_commit": commit, "start_source": source, "check": check,
                              "check_source": check_source, "generated_check": None, "baseline": None,
                              "hypotheses": [], "winner": None, "timing": {"prepare": round(time.time() - t, 2)}}

    if not check:
        if not ctx["symptom"].strip():
            raise TesterError("no check command, and no symptom to write a reproducing test from")
        t = time.time()
        repro = make_repro(ctx)
        result["timing"]["repro"] = round(time.time() - t, 2)
        ctx.update(check=repro["check"], repro_patch=repro["patch"], repro_files=repro["files"],
                   repro_files_top=repro["files_top"])
        result.update(check=repro["check"], check_source="a reproduce-it test written by the tester",
                      generated_check={k: repro[k] for k in ("check", "files", "fails_with", "seconds")})

    t = time.time()
    workers = min(MAX_PARALLEL, len(hypotheses)) if args.parallel else 1
    with ThreadPoolExecutor(max_workers=workers + 1) as pool:
        baseline = pool.submit(run_baseline, ctx) if not ctx.get("repro_patch") and not args.no_baseline else None
        if args.parallel:
            futures = [pool.submit(try_hypothesis, h, ctx) for h in hypotheses]
            results = [f.result() for f in futures]
        else:
            results = [try_hypothesis(h, ctx) for h in hypotheses]
        result["baseline"] = baseline.result() if baseline else None
    result["timing"]["hypotheses_wall"] = round(time.time() - t, 2)
    result["hypotheses"] = results

    base = result["baseline"]
    winner = next((r for r in sorted(results, key=lambda r: r["n"]) if r["passed"]), None)
    if base and base.get("passed"):
        result["verdict"] = "INCONCLUSIVE"
        result["reason"] = (f"the check `{check}` already passes in an untouched copy, so the copies cannot tell "
                            "a fix from no fix (the problem may be in this machine's setup, an ignored file "
                            "such as .env, or a running service)")
    elif winner:
        result["verdict"] = "WINNER"
        result["winner"] = winner["n"]
    else:
        result["verdict"] = "NO_WINNER"
    git(top, "worktree", "prune", check=False)
    stages = {k: [r["times"].get(k, 0) for r in results] for k in ("copy", "session", "check")}
    result["timing"].update({f"{k}_longest": max(v or [0]) for k, v in stages.items()})
    result["timing"].update({f"{k}_sum": round(sum(v), 2) for k, v in stages.items()})
    result["timing"]["total"] = round(time.time() - t0, 2)
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def first_line(result: dict[str, Any]) -> str:
    if result["verdict"] == "WINNER":
        w = next(r for r in result["hypotheses"] if r["n"] == result["winner"])
        return f"WINNER: hypothesis {w['n']} ({w['cause']})"
    if result["verdict"] == "NO_WINNER":
        return f"NO_WINNER: {len(result['hypotheses'])} hypotheses tried"
    return f"INCONCLUSIVE: {result.get('reason')}"


def report_text(result: dict[str, Any], result_path: Path | None) -> str:
    check = result.get("check") or "(none)"
    out = [first_line(result), ""]
    out.append(f"Team run: {result.get('run_id') or '(report file)'} | check: `{check}` ({result.get('check_source')})")
    out.append(f"Copies started from: {result.get('start_source')} [{str(result.get('start_commit'))[:10]}]")
    gen = result.get("generated_check")
    if gen:
        out.append(f"No check command was given, so the tester wrote a test that reproduces the problem: "
                   f"{', '.join(gen['files'])}. It fails before any fix ({gen['seconds']}s).")
    base = result.get("baseline")
    if base:
        if base.get("passed") is None:
            out.append(f"Baseline: could not run ({base.get('error')})")
        else:
            out.append(f"Baseline (untouched copy): the check {'PASSES' if base['passed'] else 'fails, as expected'} "
                       f"({base['seconds']}s)")
    for r in result["hypotheses"]:
        times = r["times"]
        out += ["", f"Hypothesis {r['n']}: {r['cause']} (rank {r['rank']}, confidence {r.get('confidence')})",
                f"  result: {r['status'].upper()}" + (f" ({r['note']})" if r.get("note") else ""),
                "  time: " + ", ".join(f"{k} {times[k]}s" for k in ("copy", "session", "check", "cleanup", "total")
                                       if k in times)]
        session = r.get("session") or {}
        if session:
            bits = [f"{session.get('turns')} turns" if session.get("turns") is not None else "",
                    f"${session['cost_usd']:.2f}" if isinstance(session.get("cost_usd"), (int, float)) else "",
                    f"error: {session['error']}" if session.get("error") else ""]
            out.append("  session: " + (", ".join(b for b in bits if b) or "finished"))
            if session.get("result"):
                out.append("  session says (not trusted): " + " ".join(session["result"].split())[:300])
        if r.get("allowed_commands"):
            out.append("  commands allowed in the copy: " + "; ".join(r["allowed_commands"]))
        if r.get("deps"):
            out.append("  dependency folders: " + ", ".join(r["deps"]))
        out.append(f"  changes: {r.get('stat') or 'none'}" + (f" ({', '.join(r['files'][:8])})" if r.get("files") else ""))
        for finding in r.get("suspect") or []:
            out.append(f"  suspect: {finding}")
        if r.get("check"):
            c = r["check"]
            state = "timed out" if c.get("timed_out") else f"exit {c.get('exit')}"
            out.append(f"  check `{c['command']}`: {state}. Last lines:")
            out += ["    " + line for line in (c.get("tail") or "(no output)").splitlines()[-12:]]

    if result["verdict"] == "WINNER":
        w = next(r for r in result["hypotheses"] if r["n"] == result["winner"])
        out += ["", f"Winning change (hypothesis {w['n']}), as a diff against the starting point above:", "```diff",
                w["diff"].rstrip() + ("\n... (cut; the full diff is in the result file)" if w.get("diff_truncated") else ""),
                "```",
                "Your files may have changed since that starting point, so apply the change by hand, not with "
                "`git apply`.",
                f"Apply these changes to the real project yourself, then rerun `{check}`. "
                "Nothing was changed in the real project."]
    elif result["verdict"] == "NO_WINNER":
        out += ["", "None of the hypotheses made the check pass in a copy. Nothing was changed in the real project. "
                    "Record it with `post_open_question`, including every hypothesis and why it failed."]
    else:
        out += ["", "Nothing was changed in the real project. Try the team's ranked fixes yourself, in order, "
                    "rerunning the check after each."]
    tm = result.get("timing", {})
    out += ["", f"Timing: prepare {tm.get('prepare', 0)}s"
                + (f", reproduce-it test {tm['repro']}s" if "repro" in tm else "")
                + f" | copy longest {tm.get('copy_longest', 0)}s, session longest {tm.get('session_longest', 0)}s, "
                  f"check longest {tm.get('check_longest', 0)}s | hypotheses (wall) {tm.get('hypotheses_wall', 0)}s"
                + f" | total {tm.get('total', 0)}s"]
    if result_path:
        out.append(f"Full result: {result_path}")
    return "\n".join(out)


def save_result(project: Path, session: str, result: dict[str, Any]) -> Path | None:
    try:
        folder = notebook.ensure_state_dir({"session_id": session, "cwd": str(project)})
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
        path = folder / f"tester_{safe}.json"
        path.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        return path
    except OSError:
        return None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Try the backup team's ranked fixes in throwaway copies of the project.")
    p.add_argument("--project", required=True, help="the project folder (the hook's message gives it)")
    p.add_argument("--session", required=True, help="the Claude Code session id")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--run-id", help="team run id: the report is fetched with GET /v1/help/{run_id}")
    src.add_argument("--report", help="a report JSON file instead (for testing)")
    p.add_argument("--check", help="the command that proves a fix (default: from the report, then the session)")
    p.add_argument("--max-hypotheses", type=int, default=MAX_HYPOTHESES)
    p.add_argument("--timeout", type=float, default=SESSION_TIMEOUT, help="seconds per hypothesis session")
    p.add_argument("--check-timeout", type=float, default=CHECK_TIMEOUT, help="seconds per check run")
    par = p.add_mutually_exclusive_group()
    par.add_argument("--parallel", dest="parallel", action="store_true", default=True,
                     help=f"test hypotheses at the same time, up to {MAX_PARALLEL} (default)")
    par.add_argument("--no-parallel", dest="parallel", action="store_false")
    p.add_argument("--deps", choices=["link", "copy", "none"], default="link",
                   help="ignored dependency folders (node_modules, .venv, venv): link (default), copy or none")
    p.add_argument("--link", action="append", help="another ignored folder to link or copy (repeatable)")
    p.add_argument("--setup", help="a command to run in each copy before the session (e.g. `npm ci`)")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--max-turns", type=int, default=MAX_TURNS)
    p.add_argument("--no-baseline", action="store_true", help="skip the check in an untouched copy")
    p.add_argument("--keep", action="store_true", help="keep the copies for debugging (prints their paths)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = Path(args.project).expanduser().resolve()
    try:
        result = run(args)
    except TesterError as exc:
        result = {"verdict": "INCONCLUSIVE", "reason": str(exc), "hypotheses": [], "run_id": args.run_id,
                  "session": args.session, "project": str(project), "timing": {}}
        print(f"INCONCLUSIVE: {exc}\n\nNothing was changed in the real project.")
        save_result(project, args.session, result)
        return 0
    except KeyboardInterrupt:
        print("INCONCLUSIVE: interrupted. Nothing was changed in the real project.")
        return 1
    finally:
        cleanup_all()
    path = save_result(project, args.session, result)
    print(report_text(result, path))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # the agent's Bash tool reads UTF-8
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
