"""Tests for struggles.py. Run from the repo root: python -m unittest discover -s plugin/hook/tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import struggles as s  # noqa: E402


def run(cmd, ok, err="", out=None, t=0.0):
    out = err if out is None else out
    return {
        "kind": "run", "t": t, "cmd": cmd, "key": s.command_key(cmd), "ok": ok,
        "err_fp": None if ok else s.fingerprint(err),
        "out_fp": s.fingerprint(out, errors_only=False),
    }


def edit(path, t=0.0):
    return {"kind": "edit", "t": t, "path": path}


def user(t=0.0):
    return {"kind": "user", "t": t}


PRISMA = "Error: Prisma Client could not locate the Query Engine for runtime \"linux-musl\".\n    at /app/node_modules/x.js:42:7"


def names(signals):
    return {sig.name for sig in signals}


class Helpers(unittest.TestCase):
    def test_command_key_drops_flags_env_and_prefixes(self):
        self.assertEqual(s.command_key("cd app && NODE_ENV=test npm run build --verbose | tail -5"), "npm run build")
        self.assertEqual(s.command_key("pytest -x tests/test_db.py"), "pytest tests/test_db.py")

    def test_fingerprint_ignores_paths_and_line_numbers(self):
        a = "Traceback...\n  File \"/home/ana/proj/db.py\", line 12\nModuleNotFoundError: No module named 'psycopg'"
        b = "Traceback...\n  File \"C:\\Users\\raj\\proj\\db.py\", line 98\nModuleNotFoundError: No module named 'psycopg'"
        self.assertEqual(s.fingerprint(a), s.fingerprint(b))

    def test_fingerprint_keeps_names_that_matter(self):
        a = "ModuleNotFoundError: No module named 'psycopg'"
        b = "ModuleNotFoundError: No module named 'redis'"
        self.assertNotEqual(s.fingerprint(a), s.fingerprint(b))

    def test_output_fingerprint_keeps_results_but_ignores_timings(self):
        self.assertNotEqual(s.fingerprint("total = 41", errors_only=False), s.fingerprint("total = 42", errors_only=False))
        self.assertEqual(s.fingerprint("5 passed in 0.31s", errors_only=False), s.fingerprint("5 passed in 0.29s", errors_only=False))

    def test_fingerprint_empty(self):
        self.assertIsNone(s.fingerprint(""))
        self.assertIsNone(s.fingerprint(None))


class Signal1SameErrorLoop(unittest.TestCase):
    def test_fires_on_third_identical_failure(self):
        h = [run("npm run build", False, PRISMA), edit("Dockerfile"), run("npm run build", False, PRISMA)]
        self.assertNotIn("same_error_loop", names(s.check_after_run(h)))
        h += [edit("Dockerfile"), run("npm run build --verbose", False, PRISMA)]
        self.assertIn("same_error_loop", names(s.check_after_run(h)))

    def test_a_pass_resets_the_count(self):
        h = [run("npm run build", False, PRISMA), run("npm run build", False, PRISMA),
             run("npm run build", True, out="ok"), run("npm run build", False, PRISMA)]
        self.assertNotIn("same_error_loop", names(s.check_after_run(h)))

    def test_probe_commands_never_count(self):
        h = [run("grep -r foo .", False, "") for _ in range(5)]
        self.assertEqual(s.check_after_run(h), [])


class Signal2WhackAMole(unittest.TestCase):
    def test_fires_on_four_failures_with_three_errors(self):
        errors = ["Error: cannot find module 'a'", "TypeError: x is undefined",
                  "Error: cannot find module 'b'", "SyntaxError: unexpected token"]
        h = []
        for err in errors:
            h += [edit("src/index.js"), run("node src/index.js", False, err)]
        self.assertIn("whack_a_mole", names(s.check_after_run(h)))

    def test_not_with_only_two_distinct_errors(self):
        h = []
        for err in ["Error: A failed", "Error: B failed", "Error: A failed", "Error: B failed"]:
            h += [edit("x.py"), run("python x.py", False, err)]
        self.assertNotIn("whack_a_mole", names(s.check_after_run(h)))


class Signal3EditRunSameOutput(unittest.TestCase):
    def test_logic_bug_same_wrong_output_after_edits_to_same_file(self):
        h = [run("python calc.py", True, out="total = 41")]
        for _ in range(3):
            h += [edit("calc.py"), run("python calc.py", True, out="total = 41")]
        self.assertIn("edit_run_same_output", names(s.check_after_run(h)))

    def test_failing_output_unchanged_after_edits(self):
        h = [run("pytest", False, "FAILED test_a - assert 1 == 2")]
        for f in ["a.py", "b.py", "c.py"]:
            h += [edit(f), run("pytest", False, "FAILED test_a - assert 1 == 2")]
        self.assertIn("edit_run_same_output", names(s.check_after_run(h)))

    def test_normal_work_unrelated_files_tests_keep_passing(self):
        h = [run("pytest", True, out="5 passed")]
        for f in ["a.py", "b.py", "c.py", "d.py"]:
            h += [edit(f), run("pytest", True, out="5 passed")]
        self.assertNotIn("edit_run_same_output", names(s.check_after_run(h)))

    def test_output_changing_is_progress(self):
        h = [run("python calc.py", True, out="total = 41")]
        for out in ["total = 42", "total = 43", "total = 44"]:
            h += [edit("calc.py"), run("python calc.py", True, out=out)]
        self.assertNotIn("edit_run_same_output", names(s.check_after_run(h)))


class Signal6UserPushback(unittest.TestCase):
    def test_phrases(self):
        for msg in ["it's still broken", "That\u2019s wrong", "nope, same error", "you already tried that", "didn't work"]:
            self.assertEqual(names(s.check_user_message(msg, [])), {"user_pushback"}, msg)

    def test_pasted_error_we_already_saw(self):
        h = [run("npm run build", False, PRISMA)]
        self.assertEqual(names(s.check_user_message(f"I got this:\n{PRISMA}", h)), {"user_pushback"})

    def test_normal_message(self):
        self.assertEqual(s.check_user_message("great, now add a login page", []), [])


class Signals4And5Stop(unittest.TestCase):
    def test_says_done_while_still_failing(self):
        h = [user(), run("npm test", False, "1 failed"), edit("a.js"), run("npm test", False, "1 failed")]
        self.assertIn("says_done_isnt", names(s.check_stop(h, "All done!")))

    def test_done_after_passing(self):
        h = [user(), run("npm test", False, "1 failed"), edit("a.js"), run("npm test", True, out="all passed")]
        self.assertEqual(s.check_stop(h, "Fixed! The tests pass now."), [])

    def test_failures_from_earlier_turns_are_ignored(self):
        h = [user(), run("npm test", False, "1 failed"), user(), edit("README.md")]
        self.assertNotIn("says_done_isnt", names(s.check_stop(h, "Updated the README.")))

    def test_unchecked_success_claim(self):
        h = [user(), run("npm test", True, out="ok"), run("npm run build", False, "Error: x"),
             edit("src/a.ts"), run("npm run build", True, out="built"), edit("src/b.ts")]
        self.assertIn("unchecked_success_claim", names(s.check_stop(h, "I've fixed the issue, it should work now.")))

    def test_claim_without_prior_failure_is_fine(self):
        h = [user(), edit("README.md")]
        self.assertEqual(s.check_stop(h, "The docs are fixed."), [])


class Judge(unittest.TestCase):
    def test_due_after_many_actions_without_progress(self):
        h = [run("npm test", False, "1 failed", t=0)] + [edit("a.js", t=i) for i in range(1, 21)]
        self.assertTrue(s.judge_due(h, now=30))

    def test_not_due_during_normal_work(self):
        h = [edit(f"f{i}.js", t=i) for i in range(30)]
        self.assertFalse(s.judge_due(h, now=10_000))

    def test_rate_limited(self):
        h = [run("npm test", False, "1 failed", t=0)] + [edit("a.js", t=i) for i in range(1, 25)]
        self.assertFalse(s.judge_due(h, now=30, last_judge=len(h) - 3))

    def test_due_after_ten_minutes(self):
        h = [run("npm test", False, "1 failed", t=0), edit("a.js", t=100)]
        self.assertTrue(s.judge_due(h, now=700))


class Decision(unittest.TestCase):
    def test_strong_alone(self):
        self.assertTrue(s.is_struggling([s.Signal("x", "strong", "")]))

    def test_one_medium_is_not_enough(self):
        self.assertFalse(s.is_struggling([s.Signal("x", "medium", ""), s.Signal("y", "weak", "")]))

    def test_two_medium(self):
        self.assertTrue(s.is_struggling([s.Signal("x", "medium", ""), s.Signal("y", "medium", "")]))


if __name__ == "__main__":
    unittest.main()
