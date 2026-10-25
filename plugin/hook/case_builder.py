"""Case file: what POST /api/help gets when the agent is stuck (docs/api-endpoints.md #9, backend/team/case_file.py).

Standard library only. Shared by the MCP `ask_for_help` tool (plugin/mcp/server.py) and the hook, which starts
the specialist team itself after the user picks "Call the specialist team" in the stuck menu (brain.py), so both
send the same case file.

The agent's own write-up (description, command, error, tried, ...) plus what it should not have to type: the
environment, package versions, short excerpts of the files it names, and the `git diff`.

Best effort: every helper gives back "" or [] on any problem, with short timeouts and size caps. The caps match
what the server keeps (case_file.py), so nothing is sent only to be cut off there. The server scrubs secrets
again, but secret files (.env, keys, ...) and git-ignored files are never read here.
"""

import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

import identity

CASE_MAX_CHARS = 30_000        # the whole case file, like case_file.MAX_TOTAL_CHARS
CASE_TIMEOUT = 2               # seconds per subprocess (git, node, python)
MAX_FILES = 5
FILE_EXCERPT_CHARS = 3_000     # per file; case_file.FILE_EXCERPT_CAP
FILE_READ_BYTES = 1_000_000    # bigger files are only read up to here
DIFF_CHARS = 8_000             # case_file.FIELD_CAPS["diff"]
ENV_CHARS = 1_500
MAX_PACKAGES = 15
MANIFEST_BYTES = 20_000_000    # a package-lock.json bigger than this is skipped
SECRET_FILE_RE = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.env|.*\.(pem|key|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|\.npmrc|"
    r"\.pypirc|\.netrc|\.git-credentials|credentials(\.json)?|secrets?\.(json|ya?ml|toml|txt))$", re.IGNORECASE)
LOCKFILE_RE = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|uv\.lock|"
                         r"Cargo\.lock|composer\.lock)$")
ARROW_RE = re.compile(r"\s*(?:->|=>|\u2192)\s*")   # "attempt -> result"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flash on Windows


def _run(args: list[str], cwd: Path) -> str:
    """A command's stdout, or "" on any problem (missing tool, timeout, non-zero exit)."""
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, timeout=CASE_TIMEOUT, creationflags=NO_WINDOW)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", errors="replace").strip() if out.returncode == 0 else ""


def _git_ignored(project: Path, rel: str) -> bool:
    try:
        out = subprocess.run(["git", "check-ignore", "-q", rel], cwd=project, capture_output=True,
                             timeout=CASE_TIMEOUT, creationflags=NO_WINDOW)
        return out.returncode == 0
    except Exception:
        return False


def parse_tried(items: list[Any] | None) -> list[dict[str, str]]:
    """"attempt -> result" (or a plain "attempt") becomes {"attempt", "result"}."""
    out = []
    for item in items or []:
        if isinstance(item, dict):
            attempt, result = str(item.get("attempt") or ""), str(item.get("result") or "")
        else:
            parts = ARROW_RE.split(str(item), maxsplit=1)
            attempt, result = parts[0], parts[1] if len(parts) > 1 else ""
        attempt, result = attempt.strip(), result.strip()
        if attempt:
            out.append({"attempt": attempt, "result": result})
    return out


def _package_versions(project: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(declared, locked) from the manifests and lockfiles in one folder: name -> "version (file)".
    `declared` are the project's own dependencies; `locked` are exact installed versions (lockfiles list every
    sub-dependency too, so a locked-only name is used only when the error shows its path)."""
    declared: dict[str, str] = {}
    locked: dict[str, str] = {}

    def read(name: str) -> str:
        path = project / name
        if not path.is_file() or path.stat().st_size > MANIFEST_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    try:
        manifest = json.loads(read("package.json") or "{}")
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            for name, version in (manifest.get(section) or {}).items():
                declared[name.lower()] = f"{version} (package.json)"
    except Exception:
        pass
    try:
        lock = json.loads(read("package-lock.json") or "{}")
        for key, info in (lock.get("packages") or {}).items():
            if key.count("node_modules/") == 1 and isinstance(info, dict) and info.get("version"):
                locked[key.split("node_modules/", 1)[1].lower()] = f"{info['version']} (package-lock.json)"
    except Exception:
        pass
    for name in sorted(p.name for p in project.glob("requirements*.txt")):
        try:
            for line in read(name).splitlines():
                match = re.match(r"\s*([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*((?:==|>=|<=|~=|!=|>|<)[^;#\s]+)", line)
                if match:
                    declared[match.group(1).lower()] = f"{match.group(2)} ({name})"
        except Exception:
            pass
    try:
        import tomllib
        pyproject = tomllib.loads(read("pyproject.toml") or "")
        for dep in (pyproject.get("project") or {}).get("dependencies") or []:
            match = re.match(r"\s*([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*([^;]*)", dep)
            if match:
                declared[match.group(1).lower()] = f"{match.group(2).strip() or 'any version'} (pyproject.toml)"
        poetry = ((pyproject.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
        for name, version in poetry.items():
            version = version.get("version", "") if isinstance(version, dict) else version
            declared[name.lower()] = f"{version} (pyproject.toml)"
        for package in tomllib.loads(read("poetry.lock") or "").get("package") or []:
            if package.get("name") and package.get("version"):
                locked[package["name"].lower()] = f"{package['version']} (poetry.lock)"
    except Exception:
        pass
    return declared, locked


def _mentioned(name: str, text: str) -> bool:
    """Is the package named in the (lowercased) text? "@prisma/client" also counts when "prisma" is named."""
    names = {name} | ({name.split("/")[0].lstrip("@")} if name.startswith("@") and "/" in name else set())
    return any(len(n) >= 3 and re.search(rf"(?<![\w.\-]){re.escape(n)}(?![\w\-])", text) for n in names)


def _packages(folders: list[Path], text: str) -> list[str]:
    """"name version (file)" for the packages the problem names: declared ones by name, locked-only ones when
    their install path (node_modules/<name>, site-packages/<name>) shows up. The locked version wins. `folders`:
    the project root first, then the folders of the files the agent named (a backend/ or web/ of a monorepo)."""
    declared: dict[str, str] = {}
    locked: dict[str, str] = {}
    for folder in folders:
        more_declared, more_locked = _package_versions(folder)
        for name, version in more_declared.items():
            declared.setdefault(name, version)
        for name, version in more_locked.items():
            locked.setdefault(name, version)
    text = text.lower().replace("\\", "/")
    names = [n for n in declared if _mentioned(n, text)]
    names += [n for n in locked if n not in declared and (f"node_modules/{n}/" in text
                                                         or f"site-packages/{n.replace('-', '_')}/" in text)]
    return [f"{n} {locked.get(n) or declared[n]}" for n in sorted(set(names))][:MAX_PACKAGES]


def _environment(project: Path, text: str, folders: list[Path]) -> str:
    """OS, Python and Node versions, and the versions of the packages the problem names."""
    lines = [f"OS: {platform.system()} {platform.release()} ({platform.machine()})"]
    venv = [project / ".venv" / "Scripts" / "python.exe", project / ".venv" / "bin" / "python"]
    for python in [str(p) for p in venv if p.is_file()] + ["python", "python3"]:
        version = _run([python, "--version"], project)
        if version.startswith("Python"):
            lines.append(f"Python: {version.split()[-1]}" + (" (project .venv)" if python not in ("python", "python3")
                                                              else ""))
            break
    node = _run(["node", "--version"], project)
    if node:
        lines.append(f"Node: {node}")
    try:
        packages = _packages(folders, text)
    except Exception:
        packages = []
    if packages:
        lines.append("Packages named in the problem: " + "; ".join(packages))
    return "\n".join(lines)[:ENV_CHARS]


def _excerpt(text: str, name: str, hints: str) -> str:
    """The part of a file the team needs: around the line the error points at, else the start."""
    lines = text.splitlines()
    match = re.search(rf"{re.escape(name)}\"?(?:, line |:|\()(\d+)", hints)
    start = max(0, int(match.group(1)) - 40) if match else 0
    excerpt = "\n".join(lines[start:])
    if start:
        excerpt = f"[from line {start + 1}]\n" + excerpt
    return excerpt if len(excerpt) <= FILE_EXCERPT_CHARS else excerpt[:FILE_EXCERPT_CHARS].rstrip() + "\n...[cut]"


def _file_excerpts(project: Path, paths: list[str] | None, hints: str) -> list[dict[str, str]]:
    """Up to MAX_FILES excerpts of files inside the project. Skips secret, ignored, binary and outside files."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        root = project.resolve()
    except Exception:
        return out
    for raw in list(dict.fromkeys(paths or []))[:MAX_FILES * 2]:
        if len(out) >= MAX_FILES:
            break
        try:
            path = Path(str(raw).strip())
            path = (path if path.is_absolute() else root / path).resolve()
            if not path.is_file() or not path.is_relative_to(root):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in seen or SECRET_FILE_RE.search(rel) or "/.git/" in f"/{rel}" or _git_ignored(root, rel):
                continue
            seen.add(rel)
            with open(path, "rb") as f:
                data = f.read(FILE_READ_BYTES)
            if b"\0" in data[:8192]:
                continue   # binary
            out.append({"path": rel, "excerpt": _excerpt(data.decode("utf-8", errors="replace"), path.name, hints)})
        except Exception:
            continue
    return out


def _diff(project: Path) -> str:
    """`git diff` of the uncommitted changes, without secret files and with lockfiles only named."""
    text = _run(["git", "diff", "HEAD", "--no-color", "--no-ext-diff"], project) \
        or _run(["git", "diff", "--no-color", "--no-ext-diff"], project)   # no commit yet
    kept = []
    for chunk in re.split(r"(?m)^(?=diff --git )", text):
        match = re.match(r"diff --git a/(.+?) b/(.+)$", chunk, re.MULTILINE)
        path = match.group(2).strip() if match else ""
        if not chunk.strip() or SECRET_FILE_RE.search(path):
            continue
        kept.append(f"diff --git a/{path} b/{path}\n(lockfile changed; diff left out)\n" if LOCKFILE_RE.search(path)
                    else chunk)
    diff = "".join(kept).strip()
    return diff if len(diff) <= DIFF_CHARS else diff[:DIFF_CHARS].rstrip() + "\n...[cut]"


def build(description: str, command: str, error: str, tried: list[Any] | None, fingerprint: str, expected: str,
          actual: str, check_description: str, files: list[str] | None, project_id: str,
          project: Path | None = None) -> dict[str, Any]:
    """The full case file for POST /api/help. The automatic parts never fail it. `project`: the folder to read
    (default identity.project_folder()); `project_id`: its id (the hook's rule, notebook.project_id)."""
    model = identity.current_model()
    case: dict[str, Any] = {
        "goal": description,
        "symptom": error or actual or description,
        "expected": expected,
        "actual": actual,
        "check_command": command,
        "check_description": check_description,
        "tried": parse_tried(tried),
        "fingerprint": fingerprint,
        "project": project_id,
        "model": "" if model == identity.UNKNOWN_MODEL else model,
    }
    project = Path(project) if project is not None else identity.project_folder()
    hints = "\n".join([description, command, error, expected, actual])
    for name, fill in (("files", lambda: _file_excerpts(project, files, hints)),
                       ("environment", lambda: _environment(project, hints, list(dict.fromkeys(
                           [project] + [project / Path(f["path"]).parent for f in case.get("files") or []])))),
                       ("diff", lambda: _diff(project))):
        try:
            case[name] = fill()
        except Exception:
            pass
    while len(json.dumps(case)) > CASE_MAX_CHARS and (case.get("diff") or case.get("files")):
        if case.get("diff"):
            case["diff"] = case["diff"][: len(case["diff"]) // 2].rstrip() + "\n...[cut]" \
                if len(case["diff"]) > 500 else ""
        else:
            case["files"].pop()
    return {k: v for k, v in case.items() if v}
