"""Tests for the backup team's replies and the local tester (docs/local-tester.md).

Run from the repo root: python -m unittest discover -s plugin/hook/tests
The hook tests send events through the brain like test_engine.py. The tester tests run
plugin/tester/test_fixes.py on small throwaway git repos with a FAKE claude (plugin/hook/dev/fake_claude.py),
never the real one.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1]
PLUGIN = HOOK.parent
sys.path.insert(0, str(HOOK))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PLUGIN / "tester"))

import brain  # noqa: E402
import checkpoints  # noqa: E402
import messages  # noqa: E402
import test_fixes  # noqa: E402
from test_engine import DC, PRISMA, TempProject, tag, text_of  # noqa: E402
from test_rewind import GitProject  # noqa: E402

FAKE = HOOK / "dev" / "fake_claude.py"
CHECK = "python check.py"
LABEL = messages.COMMUNITY_LABEL


def tester_cmd(project, sid, run_id="run-1", check=CHECK):
    return messages.tester_command(project, sid, run_id, check)


# ---------------------------------------------------------------------------
# The hook: team replies (ask_for_help / check_help)
# ---------------------------------------------------------------------------

class TeamReplies(TempProject):
    TEAM_MODE = "auto"   # the fake backend can't start the team: CALL_TEAM, and the agent calls ask_for_help itself

    def stuck(self):
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "CALL_TEAM")
        self.assertIn("TEST_FIXES", text_of(out))   # CALL_TEAM mentions the tester step

    def test_reply_parsing(self):
        cases = {
            LABEL + "TEAM_STARTED: run-7\nThe team is researching.": ("TEAM_STARTED", "run-7"),
            "TEAM_WORKING: run-7\n- docs: searching": ("TEAM_WORKING", "run-7"),
            LABEL + "TEAM_REPORT: run-7\n1. cause": ("TEAM_REPORT", "run-7"),
            "TEAM_NO_ANSWER: run-7": ("TEAM_NO_ANSWER", "run-7"),
            "TEAM_FAILED: budget": ("TEAM_FAILED", "budget"),
            LABEL + "KNOWN_FIX: post #4 (7 of 7 worked)": ("KNOWN_FIX", "post #4 (7 of 7 worked)"),
            "TRY_FIRST: checklist": ("TRY_FIRST", "checklist"),
            "[Community-posted content, another label.]\nTEAM_STARTED: r2": ("TEAM_STARTED", "r2"),
            "Team started. Run id: r1": (None, ""),
            "Could not reach the forum: refused": (None, ""),
        }
        for text, expected in cases.items():
            self.assertEqual(brain._team_reply(text), expected, text)

    def test_team_started_sets_team_ran_and_run_id(self):
        self.stuck()
        self.assertIsNone(self.mcp("ask_for_help", LABEL + "TEAM_STARTED: run-1\nKeep working."))
        episode = self.state()["episode"]
        self.assertEqual((episode["team_ran"], episode["team_run_id"], episode["team_status"]), (True, "run-1", "working"))
        self.assertIsNone(self.mcp("check_help", LABEL + "TEAM_WORKING: run-1\n- docs: searching"))
        self.assertEqual(self.state()["episode"]["team_status"], "working")

    def test_mcp_result_object_shape(self):
        self.stuck()
        self.send("PostToolUse", tool_name="mcp__plugin_breakpoint_breakpoint__ask_for_help", tool_input={},
                  tool_response={"content": [{"type": "text", "text": LABEL + "TEAM_STARTED: run-9"}]})
        self.assertEqual(self.state()["episode"]["team_run_id"], "run-9")

    def test_known_fix_and_try_first_do_not_set_team_ran(self):
        self.stuck()
        self.assertIsNone(self.mcp("ask_for_help", LABEL + "TRY_FIRST: check your own code first"))
        self.assertFalse(self.state()["episode"]["team_ran"])
        self.assertIsNone(self.mcp("ask_for_help", LABEL + "KNOWN_FIX: post #4 (7 of 7 worked)\nSolution: ..."))
        episode = self.state()["episode"]
        self.assertFalse(episode["team_ran"])
        self.assertEqual(episode["pending_fix"]["post_id"], 4)   # the hook watches the next run, as for its own fixes
        self.ok(DC)
        self.assertIn(("outcome", 4, True), self.backend.calls)
        self.assertIsNone(self.stop())

    def test_report_sends_test_fixes_with_the_exact_command_once(self):
        self.stuck()
        self.mcp("ask_for_help", LABEL + "TEAM_STARTED: run-1")
        out = self.send("PostToolUse", tool_name="mcp__plugin_breakpoint_breakpoint__check_help",
                        tool_input={"run_id": "run-1"}, tool_response=[{"type": "text", "text": LABEL +
                                                                         "TEAM_REPORT: run-1\n1. musl engine ..."}])
        self.assertEqual(tag(out), "TEST_FIXES")
        text = text_of(out)
        command = tester_cmd(self.project, self.sid, "run-1", DC)
        self.assertIn(f"tester command: {command}", text)
        self.assertIn(messages.TESTER_SCRIPT.as_posix(), command)
        self.assertTrue(Path(messages.TESTER_SCRIPT).is_file())
        self.assertIn(f'--project "{self.project.as_posix()}"', command)
        self.assertIn(f"--session {self.sid} --run-id run-1 --check 'docker compose up --build api'", command)
        self.assertIn("ask the user first", text)
        self.assertIn("timeout 600000", text)
        self.assertIn("On no: try the report's fixes yourself, in rank order", text)
        self.assertEqual(self.state()["episode"]["team_status"], "report")
        self.assertIsNone(self.mcp("check_help", "TEAM_REPORT: run-1\n1. musl engine ..."))   # same run: once

    def test_after_a_report_the_ladder_waits_before_an_open_question(self):
        self.stuck()
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        self.mcp("check_help", "TEAM_REPORT: run-1\n1. ...")
        outs = []
        for _ in range(brain.REPORT_TRIES):
            self.edit()
            outs.append(self.fail(DC, PRISMA))
        self.assertTrue(all(o is None for o in outs[:-1]))
        self.assertEqual(tag(outs[-1]), "POST_OPEN_QUESTION")

    def test_no_answer_asks_for_an_open_question(self):
        self.stuck()
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        out = self.mcp("check_help", LABEL + "TEAM_NO_ANSWER: run-1\nNothing found.")
        self.assertEqual(tag(out), "POST_OPEN_QUESTION")
        self.assertIn("command: docker compose up --build api", text_of(out))

    def test_team_failed_means_keep_working(self):
        self.stuck()
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        out = self.mcp("check_help", "TEAM_FAILED: run-1 hit the budget")
        self.assertEqual(tag(out), "TEAM_STATUS")
        self.assertIn("not an Open", text_of(out))
        self.assertFalse(self.state()["episode"]["team_ran"])
        for _ in range(3):
            self.edit()
            self.assertNotEqual(tag(self.fail(DC, PRISMA)), "POST_OPEN_QUESTION")
        self.user("ok, keep going later")
        self.assertIsNone(self.stop())

    def test_stop_while_the_team_works_reminds_check_help(self):
        self.stuck()
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        self.user("any news?")   # a new turn: NOT_DONE (failed this turn) has priority otherwise
        out = self.stop()
        self.assertEqual(tag(out), "TEAM_STATUS")
        self.assertIn("check_help", text_of(out))
        self.assertIn("run-1", text_of(out))


class DirectAskForHelp(TempProject):
    """The agent calls ask_for_help on its own, without a CALL_TEAM from the hook."""

    def test_no_prior_failure_is_tracked_and_never_blocks_stop(self):
        self.user("why is the page blank?")
        self.assertIsNone(self.mcp("ask_for_help", "TEAM_STARTED: run-1"))
        episode = self.state()["episode"]
        self.assertEqual((episode["key"], episode["team_ran"], episode["team_run_id"]), ("", True, "run-1"))
        self.mcp("check_help", "TEAM_NO_ANSWER: run-1")
        self.assertIsNone(self.stop())   # never block Stop on an episode with no command

    def test_keyless_episode_adopts_the_next_failure_and_closes_on_pass(self):
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        self.fail(DC, PRISMA)
        self.assertEqual(self.state()["episode"]["key"], brain.struggles.command_key(DC))
        self.ok(DC)
        self.assertIsNone(self.state()["episode"])
        self.assertIsNone(self.stop())

    def test_after_a_failure_then_a_pass_the_episode_closes(self):
        self.fail(DC, PRISMA)
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        episode = self.state()["episode"]
        self.assertEqual(episode["key"], brain.struggles.command_key(DC))
        self.ok(DC)
        self.assertIsNone(self.state()["episode"])
        self.assertIsNone(self.stop())

    def test_failure_already_fixed_before_the_call_is_not_its_command(self):
        self.fail(DC, PRISMA)
        self.ok(DC)
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        self.assertEqual(self.state()["episode"]["key"], "")
        self.mcp("check_help", "TEAM_NO_ANSWER: run-1")
        self.assertIsNone(self.stop())

    def test_known_fix_or_try_first_direct_do_not_set_team_ran(self):
        self.fail(DC, PRISMA)
        self.mcp("ask_for_help", "KNOWN_FIX: post #1 (9 of 10 worked)")
        self.assertFalse(self.state()["episode"]["team_ran"])
        self.mcp("ask_for_help", "TRY_FIRST: checklist")
        self.assertFalse(self.state()["episode"]["team_ran"])
        self.user("ok")
        self.assertIsNone(self.stop())


# ---------------------------------------------------------------------------
# The hook: the tester's own Bash run
# ---------------------------------------------------------------------------

class TesterRun(TempProject):
    def reported(self):
        for _ in range(3):
            self.fail(DC, PRISMA)
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        self.mcp("check_help", "TEAM_REPORT: run-1\n1. ...")

    def run_tester(self, first_line, failed=False):
        cmd = tester_cmd(self.project, self.sid, "run-1", DC)
        body = first_line + "\n\nHypothesis 1: ...\nTiming: ... total 61.0s\n"
        if failed:
            return self.send("PostToolUseFailure", tool_name="Bash", tool_input={"command": cmd},
                             error="Exit code 1\n" + body)
        return self.send("PostToolUse", tool_name="Bash", tool_input={"command": cmd},
                         tool_response={"stdout": body, "stderr": "", "interrupted": False})

    def test_tester_command_is_recognized_and_not_counted(self):
        self.assertTrue(brain._is_tester_command(tester_cmd(self.project, self.sid)))
        self.assertFalse(brain._is_tester_command("python -m pytest tests -q"))
        self.reported()
        before = len(self.state()["history"])
        self.run_tester("NO_WINNER: 3 hypotheses tried", failed=True)
        self.assertEqual(len(self.state()["history"]), before)   # not a run, never a struggle signal

    def test_winner_then_the_check_passes_credits_the_team(self):
        self.reported()
        self.assertIsNone(self.run_tester("WINNER: hypothesis 2 (Alpine needs the musl engine)"))
        episode = self.state()["episode"]
        self.assertEqual(episode["team_fix"], {"hypothesis": 2, "cause": "Alpine needs the musl engine",
                                               "run_id": "run-1"})
        self.assertEqual(episode["team_status"], "winner")
        self.edit("schema.prisma")   # the agent applies the change in the real project
        out = self.ok(DC)
        self.assertEqual(tag(out), "NOTABLE_SOLVE")
        self.assertIn("backup team's hypothesis 2 (Alpine needs the musl engine)", text_of(out))
        self.assertIn("run-1", text_of(out))
        self.assertIsNone(self.state()["episode"])

    def test_winner_does_not_trigger_open_question_while_applying(self):
        self.reported()
        self.run_tester("WINNER: hypothesis 1 (x)")
        for _ in range(3):
            self.edit()
            self.assertNotEqual(tag(self.fail(DC, PRISMA)), "POST_OPEN_QUESTION")

    def test_no_winner_asks_for_an_open_question(self):
        self.reported()
        out = self.run_tester("NO_WINNER: 3 hypotheses tried")
        self.assertEqual(tag(out), "POST_OPEN_QUESTION")
        self.assertIn("local tester", text_of(out))
        self.mcp("post_open_question", "Posted as new open question #9.")
        self.user("thanks")
        self.assertIsNone(self.stop())

    def test_inconclusive_means_try_the_fixes_yourself(self):
        self.reported()
        out = self.run_tester("INCONCLUSIVE: the check already passes in an untouched copy")
        self.assertEqual(tag(out), "TEAM_STATUS")
        self.assertIn("rank order", text_of(out))


# ---------------------------------------------------------------------------
# The tester script, with a fake claude, on real (tiny) git repos
# ---------------------------------------------------------------------------

def our_temp_dirs():
    return {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith(test_fixes.COPY_PREFIX)}


class TesterScript(GitProject):
    BUGGY = "def total(xs):\n    return sum(xs) - 1\n"
    FIXED = "def total(xs):\n    return sum(xs)\n"

    def setUp(self):
        super().setUp()
        self.write("app.py", self.BUGGY)
        self.write("check.py", "import sys\nfrom app import total\nprint('total', total([1, 2]))\n"
                               "sys.exit(0 if total([1, 2]) == 3 else 1)\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "buggy")
        # uncommitted work at the moment the problem starts: must be in the copies
        self.write("helper.py", "WIP = True\n")
        self.fail(CHECK, "Exit code 1\nError: total 2")   # the hook saves checkpoint episode_start here
        # later edits in the real project: must NOT be in the copies
        self.write("app.py", "def total(xs):\n    return 0  # later attempt\n")
        self.scratch = Path(tempfile.mkdtemp(prefix="ao_tester_test_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.scratch, True))
        self.log = self.scratch / "fake.log"
        os.environ[test_fixes.CLAUDE_ENV] = json.dumps([sys.executable, str(FAKE)])
        os.environ["BREAKPOINT_FAKE_LOG"] = str(self.log)
        self.temp_before = our_temp_dirs()

    def plan(self, plan):
        path = self.scratch / "plan.json"
        path.write_text(json.dumps({"peek": ["app.py", "helper.py"], **plan}), encoding="utf-8")
        os.environ["BREAKPOINT_FAKE_PLAN"] = str(path)

    def report(self, hypotheses, **extra):
        path = self.scratch / "report.json"
        path.write_text(json.dumps({"status": "report", "report": {"hypotheses": hypotheses, **extra}}),
                        encoding="utf-8")
        return path

    @staticmethod
    def hyp(rank, cause, steps=("change app.py",), command=None):
        return {"rank": rank, "cause": cause, "confidence": 0.5, "steps": list(steps),
                "check": {"command": command, "description": "total([1, 2]) is 3"}}

    def run_script(self, *extra, report=None):
        out = io.StringIO()
        args = ["--project", str(self.project), "--session", self.sid, "--report", str(report)] + list(extra)
        started = time.time()
        with contextlib.redirect_stdout(out):
            code = test_fixes.main(args)
        self.elapsed = time.time() - started
        return code, out.getvalue()

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def assert_cleaned_up(self, real_app="def total(xs):\n    return 0  # later attempt\n"):
        self.assertEqual(self.git("worktree", "list").strip().count("\n"), 0, self.git("worktree", "list"))
        self.assertEqual(our_temp_dirs() - self.temp_before, set())
        self.assertEqual(self.read("app.py"), real_app)                 # the real project is untouched
        self.assertTrue((self.project / "node_modules" / "lib" / "index.js").is_file())
        self.assertFalse((self.project / "notes.txt").exists())

    # -- the main path -----------------------------------------------------------------------------
    def test_winner_from_the_checkpoint_with_diff_and_timing(self):
        self.plan({"1": {"files": {"app.py": "def total(xs):\n    return sum(xs) + 5\n"}, "reply": "Fixed it!"},
                   "2": {"files": {"app.py": self.FIXED, "notes.txt": "why\n"}, "reply": "removed the - 1"},
                   "3": {"reply": "nothing to do"}})
        status_before = self.git("status", "--porcelain")
        code, out = self.run_script("--check", CHECK, report=self.report(
            [self.hyp(1, "off by five"), self.hyp(2, "stray minus one"), self.hyp(3, "nothing")]))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "WINNER: hypothesis 2 (stray minus one)")
        self.assertIn("checkpoint episode_start", out)
        self.assertIn("Baseline (untouched copy): the check fails, as expected", out)

        calls = sorted(self.calls(), key=lambda c: c["job"])
        self.assertEqual([c["job"] for c in calls], ["1", "2", "3"])
        for call in calls:
            self.assertEqual(call["peek"]["app.py"], self.BUGGY)        # from the checkpoint, not the later edit
            self.assertEqual(call["peek"]["helper.py"], "WIP = True\n")  # uncommitted work is in the copy
            self.assertEqual(call["specialist"], "1")
            self.assertIsNone(call["project_dir_env"])
            cwd = Path(call["cwd"]).resolve()
            self.assertNotIn(self.project.resolve(), [cwd, *cwd.parents])
            self.assertIn(Path(tempfile.gettempdir()).resolve(), cwd.parents)
            self.assertTrue(call["links"].get("node_modules"))           # dependency folder linked, not copied
            argv = call["argv"]
            for flag in ("-p", "--strict-mcp-config", "--no-session-persistence"):
                self.assertIn(flag, argv)
            for flag, value in (("--model", "sonnet"), ("--output-format", "json"),
                                ("--permission-mode", "acceptEdits"), ("--permission-prompts", "none"),
                                ("--max-turns", str(test_fixes.MAX_TURNS))):
                self.assertEqual(argv[argv.index(flag) + 1], value)
            self.assertNotIn("--allowedTools", argv)
            self.assertIn(f"Hypothesis {call['job']}", call["prompt"])
            self.assertIn("apply the steps of the ONE hypothesis", call["prompt"])

        # the claim "Fixed it!" in hypothesis 1 is not trusted: the script's own check says FAILED
        self.assertIn("Hypothesis 1: off by five", out)
        self.assertRegex(out, r"Hypothesis 1: off by five.*\n  result: FAILED")
        self.assertRegex(out, r"Hypothesis 2: stray minus one.*\n  result: PASSED")
        self.assertIn("-    return sum(xs) - 1\n+    return sum(xs)", out)
        self.assertIn("+++ b/notes.txt", out)                            # new files are in the change set
        self.assertNotIn("__pycache__", out)                             # files the check wrote are not
        self.assertIn("Apply these changes to the real project yourself, then rerun `python check.py`. "
                      "Nothing was changed in the real project.", out)
        self.assertRegex(out, r"Timing: prepare [\d.]+s .*copy longest [\d.]+s, session longest [\d.]+s, "
                              r"check longest [\d.]+s .*total [\d.]+s")

        saved = json.loads((self.project / ".claude" / "loop_state" / f"tester_{self.sid}.json").read_text())
        self.assertEqual((saved["verdict"], saved["winner"]), ("WINNER", 2))
        for key in ("copy", "session", "check", "total"):
            self.assertIn(key, saved["hypotheses"][1]["times"])
        self.assertIn("total", saved["timing"])
        self.assertEqual(self.git("status", "--porcelain"), status_before)
        self.assert_cleaned_up()
        print(f"\n[tester timing, fake claude, 3 hypotheses in parallel] total {saved['timing']['total']}s, "
              f"copy longest {saved['timing']['copy_longest']}s", file=sys.stderr)

    def test_no_winner(self):
        self.plan({"1": {"reply": "done"}, "2": {"files": {"app.py": "def total(xs):\n    return 7\n"}}})
        code, out = self.run_script("--check", CHECK, "--no-parallel",
                                report=self.report([self.hyp(1, "a"), self.hyp(2, "b")]))
        self.assertEqual(out.splitlines()[0], "NO_WINNER: 2 hypotheses tried")
        self.assertIn("post_open_question", out)
        self.assert_cleaned_up()

    def test_check_comes_from_the_report_and_max_hypotheses(self):
        self.plan({"1": {"files": {"app.py": self.FIXED}}})
        code, out = self.run_script("--max-hypotheses", "1", report=self.report(
            [self.hyp(2, "second"), self.hyp(1, "first", command=CHECK)]))
        self.assertEqual(out.splitlines()[0], "WINNER: hypothesis 1 (first)")   # sorted by rank, top 1 only
        self.assertIn("(hypotheses)", out)
        self.assertEqual(len(self.calls()), 1)
        self.assert_cleaned_up()

    def test_a_shortcut_that_passes_is_suspect_not_a_winner(self):
        self.plan({"1": {"files": {"check.py": "import sys\nsys.exit(0) or True  # || true\n",
                                   "app.py": "def total(xs):\n    try:\n        return sum(xs) - 1\n"
                                             "    except Exception: pass\n"}}})
        code, out = self.run_script("--check", CHECK, report=self.report([self.hyp(1, "skip it")]))
        self.assertEqual(out.splitlines()[0], "NO_WINNER: 1 hypotheses tried")
        self.assertIn("result: SUSPECT", out)
        self.assert_cleaned_up()

    def test_timeout_kills_the_session_and_still_cleans_up(self):
        self.plan({"1": {"sleep": 60}})
        code, out = self.run_script("--check", CHECK, "--timeout", "3", report=self.report([self.hyp(1, "slow")]))
        self.assertEqual(out.splitlines()[0], "NO_WINNER: 1 hypotheses tried")
        self.assertIn("timed out after 3s", out)
        self.assertLess(self.elapsed, 40)
        self.assert_cleaned_up()

    def test_baseline_already_passing_is_inconclusive(self):
        self.plan({"1": {"files": {"app.py": self.FIXED}}})
        code, out = self.run_script("--check", "python -c \"print('fine')\"", report=self.report([self.hyp(1, "x")]))
        self.assertTrue(out.startswith("INCONCLUSIVE: the check"), out[:200])
        self.assertIn("already passes in an untouched copy", out)
        self.assert_cleaned_up()

    def test_install_steps_get_exact_commands_and_a_private_dependency_copy(self):
        self.assertEqual(test_fixes.allowed_commands(
            ["Run `npm install left-pad@1.3.0`, then `npm test`", "Run `pip install requests`",
             "Run `npm install x; rm -rf /`", "Edit `src/a.js`"]),
            ["npm install left-pad@1.3.0", "npm test"])
        self.plan({"1": {"files": {"app.py": self.FIXED}}})
        code, out = self.run_script("--check", CHECK, report=self.report(
            [self.hyp(1, "pin left-pad", steps=["Run `npm install left-pad@1.3.0`"])]))
        call = self.calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--allowedTools") + 1:], ["Bash(npm install left-pad@1.3.0)", "PowerShell(npm install left-pad@1.3.0)"])
        self.assertFalse(call["links"].get("node_modules"))   # a private copy: an install can't reach the real one
        self.assertIn("node_modules (private copy)", out)
        self.assert_cleaned_up()

    def test_no_check_command_writes_a_reproducing_test(self):
        sid = "fresh-session"   # no notes: no last failing command to fall back on
        self.plan({"repro": {"files": {"repro_test.py": "import sys\nfrom app import total\n"
                                                        "sys.exit(0 if total([2, 2]) == 4 else 1)\n"},
                             "reply": "Wrote repro_test.py.\nCHECK: python repro_test.py"},
                   "1": {"files": {"app.py": self.FIXED}}})
        report = self.report([self.hyp(1, "stray minus one")], case={"symptom": "total([2, 2]) returns 3"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            test_fixes.main(["--project", str(self.project), "--session", sid, "--report", str(report)])
        out = out.getvalue()
        self.assertEqual(out.splitlines()[0], "WINNER: hypothesis 1 (stray minus one)")
        self.assertIn("the tester wrote a test that reproduces the problem: repro_test.py", out)
        self.assertIn("check: `python repro_test.py`", out)
        self.assertNotIn("+++ b/repro_test.py", out)   # the reproducing test is not part of the fix
        jobs = [c["job"] for c in self.calls()]
        self.assertEqual(jobs, ["repro", "1"])
        self.assertIn("Do not touch these files (they are the check): repro_test.py", self.calls()[1]["prompt"])
        self.assertFalse((self.project / "repro_test.py").exists())
        self.assert_cleaned_up()

    def test_not_a_git_repo_is_inconclusive(self):
        folder = Path(tempfile.mkdtemp(prefix="ao_nogit_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, True))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = test_fixes.main(["--project", str(folder), "--session", "s", "--report",
                                    str(self.report([self.hyp(1, "x")]))])
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("INCONCLUSIVE: the project must be a git repository"))

    def test_links_are_removed_before_the_copy_is_deleted(self):
        copy = test_fixes.Copy(checkpoints.repo_root(self.project), Path("."), self.git("rev-parse", "HEAD").strip(),
                               "unit")
        try:
            notes = copy.add_dependencies(self.project, "link", ["node_modules", ".env", "app.py"])
            self.assertEqual(notes, ["node_modules (shared link, read-only by rule)"])   # tracked/files never linked
            self.assertTrue((copy.workdir / "node_modules" / "lib" / "index.js").is_file())
        finally:
            copy.remove()
        self.assertFalse(copy.parent.exists())
        self.assertTrue((self.project / "node_modules" / "lib" / "index.js").is_file())
        self.assertEqual(self.git("worktree", "list").strip().count("\n"), 0)


if __name__ == "__main__":
    unittest.main()
