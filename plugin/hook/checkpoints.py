"""Checkpoints: quiet git snapshots of the project's files, so a struggling agent can be rewound (docs/rewind.md).

Used by two callers:
  brain.py   saves checkpoints while the agent works ("last_good" when the goal command passes,
             "episode_start" at the first failure of a stuck episode). Must stay fast and silent.
  rewind.py  restores one, after the user said yes. Saves "before_rewind" first so it can be undone.

How a snapshot is taken without touching the user's git state:
  1. copy the real index (git's list of tracked files) into a throwaway temp index
     (or `git read-tree HEAD` into it, or start empty in a repo with no commits);
  2. `git add -A` into the temp index: picks up edits, deletions and new untracked files, and skips
     ignored files (.env, node_modules) because it respects .gitignore;
  3. `git write-tree`, then `git commit-tree` (a commit on no branch), then `git update-ref` under a
     hidden ref: refs/agentoverflow/checkpoints/<session_id>/<n>.
The user's branch, index (staging area) and history never change. `.claude/` (the hook's notes, Claude
Code's settings) is left out of snapshots and restores, so a rewind never rolls those back.

Every function returns a plain dict ({"ok": False, "reason": ...} on failure) and never raises.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

REF_ROOT = "refs/agentoverflow/checkpoints"
MAX_PER_SESSION = 20          # older checkpoints of the session are deleted
GIT_TIMEOUT = 2.0             # seconds per git call from the hook; slower means a huge repo: skip
RESTORE_TIMEOUT = 60.0        # seconds per git call when the user asked for a rewind
MESSAGE_PREFIX = "agentoverflow checkpoint: "
EXCLUDED = [".claude"]        # never snapshotted or restored (pathspec, relative to the repo root)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"   # git's id for a tree with no files

# Our snapshot commits get a fixed identity, so they work even where user.name is not set.
IDENTITY = {
    "GIT_AUTHOR_NAME": "AgentOverflow", "GIT_AUTHOR_EMAIL": "agentoverflow@localhost",
    "GIT_COMMITTER_NAME": "AgentOverflow", "GIT_COMMITTER_EMAIL": "agentoverflow@localhost",
}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flash on Windows


class GitError(Exception):
    """A git call failed or took too long. Caught inside this module; callers only see dicts."""


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------

def _git(cwd: Path, args: list[str], timeout: float, index: str | None = None,
         stdin: str | None = None) -> str:
    """Run one git command and return its stdout. Raises GitError on failure or timeout."""
    env = {**os.environ, **IDENTITY, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    if index:
        env["GIT_INDEX_FILE"] = index
    else:
        env.pop("GIT_INDEX_FILE", None)
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, timeout=timeout,
                                input=stdin.encode("utf-8") if stdin is not None else None,
                                creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"`git {args[0]}` took longer than {timeout:g}s") from exc
    except OSError as exc:
        raise GitError(f"could not run git: {exc}") from exc
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise GitError(f"`git {args[0]}` failed: {err[-1] if err else 'exit ' + str(result.returncode)}")
    return result.stdout.decode("utf-8", errors="replace")


def _failure(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "reason": str(exc)}


def repo_root(project_dir: Path | str, timeout: float = GIT_TIMEOUT) -> Path:
    """The top folder of the git repo holding project_dir. Raises GitError if there is none."""
    return _repo(project_dir, timeout)[0]


def _repo(project_dir: Path | str, timeout: float) -> tuple[Path, Path]:
    """(repo top folder, path of the real index file), in one git call."""
    folder = Path(project_dir)
    if not folder.is_dir():
        raise GitError(f"project folder not found: {folder}")
    lines = _git(folder, ["rev-parse", "--show-toplevel", "--git-path", "index"], timeout).splitlines()
    if len(lines) < 2 or not lines[0].strip():
        raise GitError("not a git repository")
    index = Path(lines[1].strip())
    return Path(lines[0].strip()), index if index.is_absolute() else folder / index


def _head(top: Path, timeout: float) -> str | None:
    """The current commit, or None in a repo with no commits yet."""
    try:
        return _git(top, ["rev-parse", "-q", "--verify", "HEAD^{commit}"], timeout).strip() or None
    except GitError:
        return None


def _pathspec() -> list[str]:
    return ["--", "."] + [f":(exclude){p}" for p in EXCLUDED]


def session_prefix(session_id: str) -> str:
    """The hidden ref folder for one session. Only safe characters go into a ref name."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or "")) or "unknown"
    return f"{REF_ROOT}/{safe}/"


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def _snapshot_tree(top: Path, real: Path, timeout: float) -> str:
    """A git tree of the working folder as it is right now, built in a throwaway index.
    `real` is the user's index file: it is only ever read (copied), never written."""
    tmp = Path(tempfile.mkdtemp(prefix="agentoverflow_index_"))
    index = str(tmp / "index")
    try:
        if real.is_file():
            shutil.copyfile(real, index)   # keeps git's file-timestamp cache: unchanged files aren't re-read
        elif _head(top, timeout):
            _git(top, ["read-tree", "HEAD"], timeout, index=index)
        # else: no commits and nothing staged, so the temp index starts empty
        _git(top, ["add", "-A", *_pathspec()], timeout, index=index)
        return _git(top, ["write-tree"], timeout, index=index).strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _list_refs(top: Path, session_id: str, timeout: float) -> list[dict[str, Any]]:
    """This session's checkpoints, oldest first."""
    prefix = session_prefix(session_id)
    fmt = "%(refname)%00%(objectname)%00%(tree)%00%(committerdate:unix)%00%(subject)"
    out = _git(top, ["for-each-ref", f"--format={fmt}", prefix], timeout)
    items = []
    for line in out.splitlines():
        parts = line.split("\0")
        if len(parts) != 5 or not parts[0].startswith(prefix):
            continue
        ref, commit, tree, when, subject = parts
        number = ref[len(prefix):]
        if not number.isdigit():
            continue
        label = subject[len(MESSAGE_PREFIX):] if subject.startswith(MESSAGE_PREFIX) else subject
        items.append({"id": int(number), "ref": ref, "commit": commit, "tree": tree,
                      "time": float(when or 0), "label": label})
    return sorted(items, key=lambda c: c["id"])


def save(project_dir: Path | str, session_id: str, label: str, timeout: float = GIT_TIMEOUT) -> dict[str, Any]:
    """Snapshot the working folder as checkpoint `label`.

    Returns {"ok": True, "saved": True, "id", "ref", "commit", "tree", "label", "time", "pruned": [refs]},
    or "saved": False with the previous checkpoint's details when nothing changed since it,
    or {"ok": False, "reason"} (not a git repo, git too slow, ...).
    """
    try:
        top, real_index = _repo(project_dir, timeout)
        tree = _snapshot_tree(top, real_index, timeout)
        existing = _list_refs(top, session_id, timeout)
        if existing and existing[-1]["tree"] == tree:   # same files: reuse that snapshot for this label
            return {**existing[-1], "ok": True, "saved": False, "label": label, "time": time.time(),
                    "same_as": existing[-1]["label"], "pruned": []}
        head = _head(top, timeout)
        parent = ["-p", head] if head else []
        commit = _git(top, ["commit-tree", "--no-gpg-sign", tree, *parent, "-m", MESSAGE_PREFIX + label],
                      timeout).strip()
        number = existing[-1]["id"] + 1 if existing else 1
        ref = f"{session_prefix(session_id)}{number}"
        _git(top, ["update-ref", ref, commit], timeout)
        pruned = _prune(top, existing + [{"ref": ref}], timeout)
        return {"ok": True, "saved": True, "id": number, "ref": ref, "commit": commit, "tree": tree,
                "label": label, "time": time.time(), "pruned": pruned}
    except (GitError, OSError) as exc:
        return _failure(exc)


def _prune(top: Path, items: list[dict[str, Any]], timeout: float) -> list[str]:
    """Delete the oldest refs beyond MAX_PER_SESSION. Returns the deleted ref names."""
    old = [c["ref"] for c in items[:-MAX_PER_SESSION]] if len(items) > MAX_PER_SESSION else []
    if old:
        _git(top, ["update-ref", "--stdin"], timeout, stdin="".join(f"delete {r}\n" for r in old))
    return old


def list_checkpoints(project_dir: Path | str, session_id: str, timeout: float = GIT_TIMEOUT) -> dict[str, Any]:
    """{"ok": True, "checkpoints": [...oldest first]} for one session."""
    try:
        top = repo_root(project_dir, timeout)
        return {"ok": True, "checkpoints": _list_refs(top, session_id, timeout)}
    except (GitError, OSError) as exc:
        return _failure(exc)


# ---------------------------------------------------------------------------
# Comparing a checkpoint with the files now
# ---------------------------------------------------------------------------

def _diff(top: Path, old: str, new: str, timeout: float) -> dict[str, list[str]]:
    """Files that differ between two snapshots: changed, added (in new only), removed (in old only)."""
    out = _git(top, ["diff", "--name-status", "-z", "--no-renames", "--no-ext-diff", old, new, *_pathspec()],
               timeout)
    parts = out.split("\0")
    changes: dict[str, list[str]] = {"changed": [], "added": [], "removed": []}
    for status, path in zip(parts[0::2], parts[1::2]):
        bucket = {"A": "added", "D": "removed"}.get(status[:1], "changed")
        changes[bucket].append(path)
    return changes


def summary(project_dir: Path | str, checkpoints: list[str], timeout: float = GIT_TIMEOUT) -> dict[str, Any]:
    """What rewinding to each checkpoint would undo, compared with the files right now.

    Returns {"ok": True, "summaries": {checkpoint: {"changed", "added", "removed", "files"}}} where
    "added" files would be deleted by the rewind and "removed" files would come back.
    """
    try:
        top, real_index = _repo(project_dir, timeout)
        current = _snapshot_tree(top, real_index, timeout)
        result = {}
        for checkpoint in checkpoints:
            changes = _diff(top, checkpoint, current, timeout)
            result[checkpoint] = {**changes, "files": sum(len(v) for v in changes.values())}
        return {"ok": True, "summaries": result}
    except (GitError, OSError) as exc:
        return _failure(exc)


# ---------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------

def restore(project_dir: Path | str, session_id: str, checkpoint: str,
            timeout: float = RESTORE_TIMEOUT) -> dict[str, Any]:
    """Put the working folder back to `checkpoint` (a ref or commit id). Files only.

    1. save the current files as "before_rewind" (the undo point); stop if that fails;
    2. `git restore --source=<checkpoint> --worktree -- .`, which rewrites files but never the index;
    3. delete files that were created after the checkpoint (they exist in before_rewind only).
    Ignored files are never in either snapshot, so they are never touched.

    Returns {"ok": True, "commit", "before_rewind": {...save result...}, "restored": [paths],
    "recreated": [paths], "deleted": [paths], "unchanged": bool} or {"ok": False, "reason"}.
    """
    try:
        top = repo_root(project_dir, timeout)
        commit = _git(top, ["rev-parse", "-q", "--verify", f"{checkpoint}^{{commit}}"], timeout).strip()
    except GitError:
        return {"ok": False, "reason": f"checkpoint not found: {checkpoint}"}
    before = save(project_dir, session_id, "before_rewind", timeout)
    if not before.get("ok"):
        return {"ok": False, "reason": f"could not save the undo point, so nothing was changed: {before.get('reason')}"}
    try:
        changes = _diff(top, commit, before["commit"], timeout)
        result = {"ok": True, "commit": commit, "before_rewind": before, "restored": changes["changed"],
                  "recreated": changes["removed"], "deleted": [], "unchanged": not any(changes.values())}
        if changes["changed"] or changes["removed"]:
            _git(top, ["restore", f"--source={commit}", "--worktree", *_pathspec()], timeout)
        result["deleted"] = _delete_new_files(top, changes["added"])
        return result
    except (GitError, OSError) as exc:
        return {"ok": False, "reason": f"{exc}. To get back the files as they were, rewind to before_rewind.",
                "before_rewind": before}


def _delete_new_files(top: Path, paths: list[str]) -> list[str]:
    """Remove files created after the checkpoint, plus folders they leave empty. Stays inside the repo."""
    root = top.resolve()
    deleted = []
    for rel in paths:
        path = (top / rel)
        try:
            resolved = path.parent.resolve() / path.name
            if root not in resolved.parents:
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
        except OSError:
            continue
        if path.is_symlink() or path.exists():
            continue   # a folder (e.g. a nested repo): left alone
        deleted.append(rel)   # deleted here, or already removed by `git restore` (it was in the index)
        parent = resolved.parent
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()   # only succeeds when empty
            except OSError:
                break
            parent = parent.parent
    return deleted
