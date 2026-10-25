"""Tests for the rewind feature (docs/rewind.md): checkpoints.py, the brain's offer, and rewind.py.

Run from the repo root: python -m unittest discover -s plugin/hook/tests
Every test that needs git builds a small throwaway repo with tracked, new, staged and ignored files.
"""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOK))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain  # noqa: E402
import checkpoints  # noqa: E402
import messages  # noqa: E402
import notebook  # noqa: E402
import struggles  # noqa: E402
from test_engine import TempProject, tag, text_of  # noqa: E402

PT = "python -m pytest tests -q"
GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
       "-c", "core.autocrlf=false"]
ERRORS = ["Error: alpha broke", "Error: beta broke", "Error: gamma broke", "Error: delta broke"]


def _force_remove(func, path, _exc):
    """Windows: git object files are read-only, so make them writable before deleting."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


class GitProject(TempProject):
    """A git repo with: app.py and util.py committed, .env and node_modules ignored."""

    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, self.project, onexc=_force_remove)
        self.git("init", "-q", "-b", "main")
        self.write(".gitignore", ".env\nnode_modules/\ntranscript.jsonl\n")
        self.write("app.py", "def total(xs):\n    return sum(xs)\n")
        self.write("util.py", "HELPER = 1\n")
        self.write(".env", "SECRET=1\n")
        self.write("node_modules/lib/index.js", "module.exports = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def git(self, *args):
        result = subprocess.run(GIT + list(args), cwd=self.project, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def write(self, rel, text):
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def read(self, rel):
        return (self.project / rel).read_text(encoding="utf-8")

    def git_state(self):
        """Everything a rewind must never change: branch, HEAD, history, the staging area."""
        return (self.git("branch", "--show-current"), self.git("rev-parse", "HEAD"),
                self.git("log", "--oneline", "main"), self.git("diff", "--cached", "--name-status"),
                self.git("stash", "list"))

    # -- a typical struggle: pass, then 4 edit -> fail rounds with different errors --------------
    def struggle(self, rounds=4, pass_first=True, edit=True):
        if pass_first:
            self.assertIsNone(self.ok(PT, "1 passed"))
        out = None
        for n in range(rounds):
            if edit:
                self.write("app.py", f"def total(xs):\n    return sum(xs) + {n + 1}\n")
                self.edit("app.py")
            if n == 1 and edit:
                self.write(f"scratch_{n}.py", "print('debug')\n")   # a new file made along the way
            out = self.fail(PT, "Exit code 1\n" + ERRORS[n % len(ERRORS)])
        return out


# ---------------------------------------------------------------------------
# checkpoints.py
# ---------------------------------------------------------------------------

class Checkpoints(GitProject):
    def test_save_skips_duplicates_and_numbers_refs(self):
        first = checkpoints.save(self.project, self.sid, "last_good")
        self.assertTrue(first["ok"] and first["saved"])
        self.assertEqual(first["ref"], f"refs/agentoverflow/checkpoints/{self.sid}/1")
        again = checkpoints.save(self.project, self.sid, "episode_start")
        self.assertEqual((again["saved"], again["ref"], again["same_as"]), (False, first["ref"], "last_good"))
        self.write("app.py", "changed\n")
        second = checkpoints.save(self.project, self.sid, "episode_start")
        self.assertEqual((second["saved"], second["id"]), (True, 2))
        listed = checkpoints.list_checkpoints(self.project, self.sid)["checkpoints"]
        self.assertEqual([(c["id"], c["label"]) for c in listed], [(1, "last_good"), (2, "episode_start")])
        self.assertIn("agentoverflow checkpoint: last_good", self.git("log", "--format=%s", "-1", first["ref"]))
        self.assertNotIn("agentoverflow", self.git("log", "--oneline", "main"))   # never on the branch

    def test_prune_keeps_the_newest(self):
        with mock.patch.object(checkpoints, "MAX_PER_SESSION", 3):
            results = []
            for n in range(5):
                self.write("app.py", f"v{n}\n")
                results.append(checkpoints.save(self.project, self.sid, "last_good"))
        self.assertEqual(results[3]["pruned"], [f"refs/agentoverflow/checkpoints/{self.sid}/1"])
        listed = checkpoints.list_checkpoints(self.project, self.sid)["checkpoints"]
        self.assertEqual([c["id"] for c in listed], [3, 4, 5])

    def test_restore_rewinds_files_only(self):
        self.write("staged.py", "staged = True\n")
        self.git("add", "staged.py")                 # something in the user's staging area
        saved = checkpoints.save(self.project, self.sid, "last_good")
        before = self.git_state()

        self.write("app.py", "broken\n")             # tracked file changed
        (self.project / "util.py").unlink()          # tracked file deleted
        self.write("newpkg/helper.py", "x = 1\n")    # new untracked file in a new folder
        self.write("staged.py", "changed after\n")
        self.write(".env", "SECRET=2\n")             # ignored: must survive as is
        self.write("node_modules/lib/index.js", "module.exports = 2\n")
        self.write(".claude/settings.local.json", "{}")   # Claude Code's own files: never touched

        result = checkpoints.restore(self.project, self.sid, saved["ref"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("app.py"), "def total(xs):\n    return sum(xs)\n")
        self.assertEqual(self.read("util.py"), "HELPER = 1\n")
        self.assertEqual(self.read("staged.py"), "staged = True\n")
        self.assertFalse((self.project / "newpkg").exists())
        self.assertEqual(self.read(".env"), "SECRET=2\n")
        self.assertEqual(self.read("node_modules/lib/index.js"), "module.exports = 2\n")
        self.assertTrue((self.project / ".claude" / "settings.local.json").exists())
        self.assertEqual(self.git_state(), before)   # branch, HEAD, history, index, stash unchanged
        self.assertEqual(sorted(result["restored"]), ["app.py", "staged.py"])
        self.assertEqual(result["recreated"], ["util.py"])
        self.assertEqual(result["deleted"], ["newpkg/helper.py"])
        self.assertEqual(result["before_rewind"]["label"], "before_rewind")

    def test_undo_with_before_rewind(self):
        saved = checkpoints.save(self.project, self.sid, "last_good")
        self.write("app.py", "messy\n")
        self.write("new.py", "new\n")
        first = checkpoints.restore(self.project, self.sid, saved["ref"])
        self.assertFalse((self.project / "new.py").exists())
        undo = checkpoints.restore(self.project, self.sid, first["before_rewind"]["ref"])
        self.assertTrue(undo["ok"], undo)
        self.assertEqual((self.read("app.py"), self.read("new.py")), ("messy\n", "new\n"))
        self.assertEqual(undo["recreated"], ["new.py"])

    def test_restore_when_nothing_changed(self):
        saved = checkpoints.save(self.project, self.sid, "last_good")
        result = checkpoints.restore(self.project, self.sid, saved["ref"])
        self.assertTrue(result["ok"] and result["unchanged"])

    def test_summary_counts_what_a_rewind_would_undo(self):
        saved = checkpoints.save(self.project, self.sid, "last_good")
        self.write("app.py", "changed\n")
        self.write("extra.py", "x\n")
        (self.project / "util.py").unlink()
        info = checkpoints.summary(self.project, [saved["ref"]])["summaries"][saved["ref"]]
        self.assertEqual((info["changed"], info["added"], info["removed"], info["files"]),
                         (["app.py"], ["extra.py"], ["util.py"], 3))

    def test_unknown_checkpoint_and_slow_git(self):
        self.assertFalse(checkpoints.restore(self.project, self.sid, "refs/agentoverflow/nope")["ok"])
        slow = checkpoints.save(self.project, self.sid, "last_good", timeout=0.0001)
        self.assertFalse(slow["ok"])
        self.assertIn("took longer", slow["reason"])

    def test_repo_without_commits(self):
        shutil.rmtree(self.project / ".git", onexc=_force_remove)
        self.git("init", "-q")
        saved = checkpoints.save(self.project, self.sid, "episode_start")
        self.assertTrue(saved["ok"], saved)
        self.write("app.py", "changed\n")
        self.write("brand_new.py", "x\n")
        result = checkpoints.restore(self.project, self.sid, saved["ref"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.read("app.py"), "def total(xs):\n    return sum(xs)\n")
        self.assertFalse((self.project / "brand_new.py").exists())


class NotGit(TempProject):
    def test_everything_is_a_quiet_no_op(self):
        (self.project / "a.py").write_text("x")
        self.assertFalse(checkpoints.save(self.project, self.sid, "last_good")["ok"])
        self.assertFalse(checkpoints.restore(self.project, self.sid, "HEAD")["ok"])
        self.assertFalse(checkpoints.summary(self.project, ["HEAD"])["ok"])
        self.assertFalse(checkpoints.list_checkpoints(self.project, self.sid)["ok"])
        self.assertFalse((self.project / ".git").exists())

    def test_no_offer_and_no_checkpoints_without_git(self):
        self.ok(PT, "1 passed")
        for n in range(4):
            self.edit("app.py")
            out = self.fail(PT, "Exit code 1\n" + ERRORS[n])
        self.assertEqual(tag(out), "CALL_TEAM")
        self.assertEqual(self.state()["checkpoints"], [])


# ---------------------------------------------------------------------------
# The notebook and struggles.py
# ---------------------------------------------------------------------------

class NotesAndSignals(unittest.TestCase):
    def test_checkpoint_indexes_move_with_the_history_trim(self):
        state = {"history": [], "episode": None, "last_judge": None, "checkpoints": []}
        notebook.add_checkpoint(state, {"id": 1, "ref": "r1", "time": 1.0}, "last_good", 150)
        for i in range(250):
            notebook.record(state, {"kind": "read", "t": float(i), "path": "f"})
        self.assertEqual(state["checkpoints"][0]["history_idx"], 100)

    def test_lookup_by_name_and_edits_since(self):
        state = {"history": [{"kind": "run", "key": "pytest", "ok": False}, {"kind": "edit", "path": "a.py"},
                             {"kind": "edit", "path": "a.py"}, {"kind": "edit", "path": "b.py"}],
                 "episode": notebook.new_episode("pytest", "fp", 0), "checkpoints": []}
        notebook.add_checkpoint(state, {"id": 1, "ref": "r1", "time": 1.0}, "last_good", -1)
        notebook.add_checkpoint(state, {"id": 2, "ref": "r2", "time": 2.0}, "episode_start", 0, {"key": "pytest"})
        notebook.add_checkpoint(state, {"id": 3, "ref": "r3", "time": 3.0}, "episode_start", 2, {"key": "other"})
        self.assertEqual(notebook.checkpoint_for(state, "episode_start")["ref"], "r2")   # the episode's own
        self.assertEqual(notebook.checkpoint_for(state, "last_good")["ref"], "r1")
        self.assertEqual(notebook.checkpoint_for(state, "3")["ref"], "r3")
        self.assertIsNone(notebook.checkpoint_for(state, "before_rewind"))
        self.assertEqual(notebook.edits_since(state, notebook.checkpoint_for(state, "last_good")),
                         (3, ["a.py", "b.py"]))
        state["history"].insert(2, {"kind": notebook.REWIND_KIND})   # edits before a rewind were undone
        self.assertEqual(notebook.edits_since(state, notebook.checkpoint_for(state, "last_good"))[0], 2)
        notebook.add_checkpoint(state, {"id": 4, "ref": "r4", "time": 4.0, "pruned": ["r1"]}, "x", 4)
        self.assertEqual([e["ref"] for e in state["checkpoints"]], ["r2", "r3", "r4"])

    def test_a_rewind_resets_the_signals(self):
        history = []
        for n in range(4):
            history += [{"kind": "edit", "path": "a.py"},
                        {"kind": "run", "key": "pytest", "ok": False, "err_fp": f"fp{n}", "out_fp": f"o{n}", "t": 0}]
        self.assertIn("whack_a_mole", [s.name for s in struggles.check_after_run(history)])
        history.append({"kind": "rewind", "t": 0})
        history.append({"kind": "run", "key": "pytest", "ok": False, "err_fp": "fp9", "out_fp": "o9", "t": 0})
        self.assertEqual(struggles.check_after_run(history), [])
        self.assertTrue(struggles.judge_due(history[:8] + [{"kind": "read", "t": 0}] * 20, 1.0))
        self.assertFalse(struggles.judge_due(history + [{"kind": "read", "t": 0}] * 5, 1.0))


# ---------------------------------------------------------------------------
# The brain: when checkpoints are saved and when the offer appears
# ---------------------------------------------------------------------------

class Offer(GitProject):
    def labels(self):
        return [e["label"] for e in self.state()["checkpoints"]]

    def test_checkpoints_are_saved_at_the_right_moments(self):
        self.ok(PT, "1 passed")
        self.assertEqual(self.labels(), ["last_good"])
        self.ok("python scripts/report.py", "done")      # not a goal command: no checkpoint
        self.write("app.py", "broken\n")
        self.edit("app.py")
        self.fail(PT, "Exit code 1\nError: alpha broke")  # first failure: where an episode would start
        self.assertEqual(self.labels(), ["last_good", "episode_start"])
        self.write("app.py", "worse\n")
        self.fail(PT, "Exit code 1\nError: beta broke")   # later failures: nothing new
        self.assertEqual(self.labels(), ["last_good", "episode_start"])
        entry = self.state()["checkpoints"][-1]
        self.assertEqual((entry["key"], entry["history_idx"]), ("python pytest tests", 3))
        self.write("app.py", "fixed\n")
        self.ok(PT, "1 passed")
        self.assertEqual(self.labels()[-1], "last_good")
        self.assertEqual(self.state()["checkpoints"][-1]["cmd"], PT)

    def test_offer_after_going_in_circles(self):
        out = self.struggle()
        self.assertEqual(tag(out), "REWIND_OFFER")
        text = text_of(out)
        self.assertIn("1. last_good: back to the last time `python -m pytest tests -q` passed", text)
        self.assertIn("2. episode_start: back to when this problem started", text)
        self.assertIn("Undoes 4 edits", text)
        self.assertIn("Undoes 3 edits", text)
        self.assertIn("deletes 1 file created since (scratch_1.py)", text)
        self.assertIn("already tried: first run failed", text)
        self.assertIn("never rewind without a yes", text)
        self.assertIn("Files only", text)
        script = messages.REWIND_SCRIPT.as_posix()
        self.assertIn(f'command: python "{script}" --project "{self.project.as_posix()}" --session {self.sid} '
                      "last_good", text)
        self.assertIn("--session test-session episode_start", text)
        self.assertTrue(self.state()["episode"]["rewind_offered"])
        # the next nudge carries on up the ladder, and the offer never comes back this episode
        outs = []
        for n in range(4):
            self.edit("app.py")
            outs.append(tag(self.fail(PT, "Exit code 1\n" + ERRORS[n])))
        self.assertIn("CALL_TEAM", outs)
        self.assertNotIn("REWIND_OFFER", outs)

    def test_same_error_loop_gets_no_offer(self):
        self.ok(PT, "1 passed")
        for n in range(3):
            self.write("app.py", f"v{n}\n")
            self.edit("app.py")
            out = self.fail(PT, "Exit code 1\nError: always the same")
        self.assertEqual(tag(out), "CALL_TEAM")

    def test_too_few_edits_gets_no_offer(self):
        self.ok(PT, "1 passed")
        self.fail(PT, "Exit code 1\n" + ERRORS[0])
        self.fail(PT, "Exit code 1\n" + ERRORS[1])
        for n in (2, 3):
            self.write("app.py", f"v{n}\n")
            self.edit("app.py")
            out = self.fail(PT, "Exit code 1\n" + ERRORS[n])
        self.assertEqual(tag(out), "CALL_TEAM")

    def test_only_episode_start_when_it_never_passed(self):
        out = self.struggle(pass_first=False)
        self.assertEqual(tag(out), "REWIND_OFFER")
        self.assertIn("since this problem started", text_of(out).splitlines()[0])
        self.assertNotIn("last_good:", text_of(out))

    def test_ai_judge_can_trigger_the_offer(self):
        self.backend.judge_result = {"stuck": True, "reason": "edits keep circling", "confidence": 0.9}
        self.ok(PT, "1 passed")
        self.fail(PT, "Exit code 1\nError: alpha broke")
        for n in range(20):
            self.write("app.py", f"v{n}\n")
            self.edit("app.py")
        out = self.fail(PT, "Exit code 1\nError: alpha broke")
        self.assertEqual(tag(out), "REWIND_OFFER")
        self.assertIn("AI judge: edits keep circling", text_of(out))

    def test_own_rewind_command_is_ignored(self):
        cmd = messages.rewind_command(self.project, self.sid, "last_good")
        self.assertIsNone(self.ok(cmd, "[AgentOverflow: CLEAN_SLATE] ..."))
        self.assertIsNone(self.send("PostToolUseFailure", tool_name="Bash", tool_input={"command": cmd},
                                    error="Exit code 1\nNo checkpoint"))
        self.assertEqual((self.state()["checkpoints"], self.state()["history"]), ([], []))


# ---------------------------------------------------------------------------
# rewind.py end to end, the way the agent runs it
# ---------------------------------------------------------------------------

class Cli(GitProject):
    def run_command(self, command):
        """Run a command line from a hook message, as the agent's Bash tool would."""
        args = shlex.split(command)
        self.assertEqual(args[0], "python")
        result = subprocess.run([sys.executable, *args[1:]], capture_output=True, text=True, timeout=120,
                                env={**os.environ, "CLAUDE_PROJECT_DIR": ""})
        return result

    def offered_command(self, out, name):
        line = next(ln for ln in text_of(out).splitlines() if ln.strip().startswith("command: python")
                    and ln.rstrip().endswith(name))
        return line.split("command: ", 1)[1]

    def test_rewind_note_state_and_undo(self):
        out = self.struggle()
        self.assertEqual(tag(out), "REWIND_OFFER")
        messy = self.read("app.py")

        result = self.run_command(self.offered_command(out, "last_good"))
        self.assertEqual(result.returncode, 0, result.stderr)
        note = result.stdout
        self.assertTrue(note.startswith("[AgentOverflow: CLEAN_SLATE] Files rewound to last_good: the last time "
                                        "`python -m pytest tests -q` passed"), note)
        self.assertIn("restored: 1 file (app.py)", note)
        self.assertIn("deleted (created after that point): scratch_1.py", note)
        self.assertIn("do not repeat them", note)
        self.assertIn("first run failed: Error: alpha broke", note)
        self.assertIn("Start from here with a different approach", note)
        self.assertIn("Then rerun `python -m pytest tests -q`.", note)
        self.assertIn("--session test-session before_rewind", note)
        self.assertEqual(self.read("app.py"), "def total(xs):\n    return sum(xs)\n")
        self.assertFalse((self.project / "scratch_1.py").exists())
        self.assertEqual(self.read(".env"), "SECRET=1\n")

        state = self.state()
        self.assertEqual(state["history"][-1]["kind"], "rewind")
        self.assertTrue(state["episode"]["rewound"])
        self.assertIn("rewound the files to last_good", state["episode"]["tried"])
        self.assertEqual(state["checkpoints"][-1]["label"], "before_rewind")

        # The hook ignores the rewind command itself, and old attempts no longer count
        self.assertIsNone(self.ok(self.offered_command(out, "last_good"), note))
        self.write("app.py", "new idea\n")
        self.edit("app.py")
        self.assertIsNone(self.fail(PT, "Exit code 1\n" + ERRORS[0]))

        # Undo: back to the messy files, including the new one
        undo = self.run_command(messages.rewind_command(self.project, self.sid, "before_rewind"))
        self.assertEqual(undo.returncode, 0, undo.stderr)
        self.assertEqual(self.read("app.py"), messy)
        self.assertTrue((self.project / "scratch_1.py").exists())

        listed = self.run_command(messages.rewind_command(self.project, self.sid, "--list"))
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("last_good", listed.stdout)
        self.assertIn("<- before_rewind", listed.stdout)

    def test_failures_exit_non_zero(self):
        missing = self.run_command(messages.rewind_command(self.project, self.sid, "last_good"))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("No checkpoint 'last_good'", missing.stderr)
        plain = Path(tempfile.mkdtemp(prefix="ao_plain_"))
        self.addCleanup(shutil.rmtree, plain, True)
        not_git = self.run_command(messages.rewind_command(plain, self.sid, "--list"))
        self.assertEqual(not_git.returncode, 1)
        self.assertIn("Cannot list checkpoints", not_git.stderr)

    def test_falls_back_to_git_refs_without_notes(self):
        saved = checkpoints.save(self.project, self.sid, "last_good")
        self.write("app.py", "messy\n")
        by_name = self.run_command(messages.rewind_command(self.project, self.sid, "last_good"))
        self.assertEqual(by_name.returncode, 0, by_name.stderr)
        self.assertEqual(self.read("app.py"), "def total(xs):\n    return sum(xs)\n")
        self.write("app.py", "messy again\n")
        by_id = self.run_command(messages.rewind_command(self.project, self.sid, str(saved["id"])))
        self.assertEqual(by_id.returncode, 0, by_id.stderr)
        self.assertIn("Files rewound to 1: the last time the goal command passed", by_id.stdout.splitlines()[0])
        self.assertEqual(self.read("app.py"), "def total(xs):\n    return sum(xs)\n")


if __name__ == "__main__":
    unittest.main()
