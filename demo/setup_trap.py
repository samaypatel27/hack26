"""Make a fresh, git-ready copy of a demo trap task.

Usage:
    python demo/setup_trap.py <trap-name> <dest>
    python demo/setup_trap.py --list

Example:
    python demo/setup_trap.py node-esm-shadow C:\\temp\\trap1

It copies demo/trap-tasks/<trap-name> to <dest> (without SOLUTION.md, so the agent can't read
the answer), runs `git init`, and makes one commit. The original folder stays clean. The rewind
and the local tester both need git, which is why the copy is a git repo.
Standard library only.
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TRAPS = HERE / "trap-tasks"
# Files the agent must never see, and junk that should not be copied.
SKIP = shutil.ignore_patterns("SOLUTION.md", "__pycache__", ".pytest_cache", "node_modules", ".git", "*.pyc")


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """rmtree error handler: git marks its object files read-only on Windows. Clear the flag, retry once."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def remove_tree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, onerror=_clear_readonly_and_retry)


def inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def trap_names() -> list[str]:
    return sorted(p.name for p in TRAPS.iterdir() if p.is_dir())


def git(dest: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=dest, check=True, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy a trap task to a new folder and commit it to a new git repo.")
    parser.add_argument("name", nargs="?", help="trap folder name under demo/trap-tasks/")
    parser.add_argument("dest", nargs="?", help="new folder to create (must not exist, or be empty)")
    parser.add_argument("--list", action="store_true", help="list the trap names and exit")
    parser.add_argument("--force", action="store_true", help="delete <dest> first if it already exists")
    args = parser.parse_args()

    if args.list or not args.name:
        print("Trap tasks:", ", ".join(trap_names()))
        return 0 if args.list else 2
    src = TRAPS / args.name
    if not src.is_dir():
        print(f"No trap named {args.name!r}. Choose one of: {', '.join(trap_names())}")
        return 2
    if not args.dest:
        print("Give a destination folder, for example C:\\temp\\trap1")
        return 2

    dest = Path(args.dest).expanduser().resolve()
    # Safety: never create (or delete) a copy inside this repo, and never delete the repo or a parent of it.
    if inside(dest, REPO) or inside(REPO, dest):
        print(f"Refusing: {dest} is inside the Breakpoint repo (or contains it). Use a folder like C:\\temp\\trap1.")
        return 2
    if dest.exists() and not dest.is_dir():
        print(f"{dest} exists and is a file. Pick a new folder.")
        return 2
    if dest.exists() and any(dest.iterdir()):
        if not args.force:
            print(f"{dest} already exists and is not empty. Pick a new folder or add --force.")
            return 2
        remove_tree(dest)
    if dest.exists():
        dest.rmdir()
    shutil.copytree(src, dest, ignore=SKIP)

    git(dest, "init", "-q")
    git(dest, "add", "-A")
    # A fixed local identity, so the commit works on machines without a global git user.
    git(dest, "-c", "user.name=Breakpoint Demo", "-c", "user.email=demo@example.invalid",
        "commit", "-q", "-m", "Starting point for the demo")

    task = (dest / "TASK.md").read_text(encoding="utf-8") if (dest / "TASK.md").exists() else ""
    print(f"Ready: {dest}")
    print(f"  cd \"{dest}\"")
    print("  claude --plugin-dir \"<repo>\\plugin\"     (or plain `claude` for a run without the plugin)")
    if task:
        print("\nPaste this as the request (from TASK.md):\n")
        print(task.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
