"""Tests for the hook engine: notebook.py, backend.py, messages.py, brain.py, main.py.

Run from the repo root: python -m unittest discover -s plugin/hook/tests
The brain tests use a fake backend object; the backend and end-to-end tests use a tiny local HTTP server.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

HOOK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOK))

import backend  # noqa: E402
import brain  # noqa: E402
import messages  # noqa: E402
import notebook  # noqa: E402
import struggles  # noqa: E402

ENV_KEYS = ["CLAUDE_PROJECT_DIR", "BREAKPOINT_API_URL", "BREAKPOINT_API_KEY", "API_URL", "API_KEY",
            "BREAKPOINT_SPECIALIST", "AGENTOVERFLOW_API_URL", "AGENTOVERFLOW_API_KEY", "AGENTOVERFLOW_SPECIALIST",
            "BREAKPOINT_TEAM_MODE"]

PRISMA = ('Exit code 1\nPrismaClientInitializationError: Prisma Client could not locate the Query Engine for runtime '
          '"linux-musl-openssl-3.0.x".\n    at /app/node_modules/@prisma/client/runtime/library.js:111:12')
PYTEST = ("Exit code 1\n    def test_total():\n>       assert cart_total([10, 20, 12]) == 42\nE       assert 41 == 42\n"
          "tests/test_cart.py:5: AssertionError\nFAILED tests/test_cart.py::test_total - assert 41 == 42\n1 failed in 0.05s")
DC = "docker compose up --build api"
PT = "python -m pytest tests -q"

SOLVED = {"id": 1, "description": "Prisma cannot find the Query Engine in Alpine\nmore text", "value": "v",
          "solution": "Add binaryTargets and run prisma generate in the Dockerfile.", "tags": ["prisma"],
          "upvotes": 3, "status": "solved", "useful_count": 9, "match": "fingerprint", "score": 1.0}
OPEN = {"id": 3, "description": "pytest cannot import app\nTried: PYTHONPATH", "value": "v", "solution": None,
        "tags": ["python"], "upvotes": 1, "status": "open", "useful_count": 0,
        "match": "fingerprint", "score": 1.0}


def tag(output):
    """The first [Breakpoint: TAG] in a hook output, or None."""
    if not output:
        return None
    text = output.get("reason") or output["hookSpecificOutput"]["additionalContext"]
    return text.split("]")[0].replace("[Breakpoint: ", "")


def text_of(output):
    return output.get("reason") or output["hookSpecificOutput"]["additionalContext"]


class FakeBackend:
    def __init__(self, search=None, judge=None, help=None):
        self.search_result, self.judge_result, self.help_result = search, judge, help
        self.calls = []
        self.cases = []   # the case files sent to POST /api/help

    def ask_help(self, case):
        self.calls.append(("help", case.get("check_command"), case.get("fingerprint")))
        self.cases.append(case)
        return self.help_result   # None: the request failed

    def search(self, error, command, fingerprint, tags=None):
        self.calls.append(("search", command, fingerprint))
        return self.search_result

    def report_outcome(self, post_id, worked, fingerprint, command, session_id):
        self.calls.append(("outcome", post_id, worked))
        return {"post_status": "solved"}

    def sync_identity(self, payload):
        self.calls.append(("sync", payload.get("source")))
        return {"agent_id": "x"}

    def judge(self, session_id, actions, replies):
        self.calls.append(("judge", len(actions), replies))
        return self.judge_result


class TempProject(unittest.TestCase):
    """A fresh project folder per test, with the environment cleaned of our settings."""
    TEAM_MODE = "ask"   # set explicitly, so a team_mode in the real ~/.breakpoint/config.json never matters

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="ao_test_"))
        self.addCleanup(shutil.rmtree, self.project, True)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)
        os.environ["BREAKPOINT_TEAM_MODE"] = self.TEAM_MODE
        self.transcript = self.project / "transcript.jsonl"
        self.sid = "test-session"
        self.backend = FakeBackend(search=[])

    # -- sending events through the brain, the way main.py does -------------------------------
    def send(self, event, **fields):
        payload = {"hook_event_name": event, "session_id": self.sid, "cwd": str(self.project),
                   "transcript_path": str(self.transcript), **fields}
        handler = {"SessionStart": brain.on_session_start, "UserPromptSubmit": brain.on_user_prompt,
                   "PostToolUse": brain.on_tool_use, "PostToolUseFailure": brain.on_tool_use,
                   "PreToolUse": brain.on_pre_tool_use, "Stop": brain.on_stop}[event]
        state = notebook.load(payload)
        output = handler(payload, state, self.backend)
        notebook.save(payload, state)
        return output

    def fail(self, cmd, err):
        return self.send("PostToolUseFailure", tool_name="Bash", tool_input={"command": cmd}, error=err)

    def ok(self, cmd, out="ok"):
        return self.send("PostToolUse", tool_name="Bash", tool_input={"command": cmd},
                         tool_response={"stdout": out, "stderr": "", "interrupted": False})

    def edit(self, path="src/a.py"):
        return self.send("PostToolUse", tool_name="Edit", tool_input={"file_path": path}, tool_response={})

    def user(self, prompt):
        return self.send("UserPromptSubmit", prompt=prompt)

    def mcp(self, name, response):
        return self.send("PostToolUse", tool_name=f"mcp__plugin_breakpoint_breakpoint__{name}",
                         tool_input={}, tool_response=[{"type": "text", "text": response}])

    def stop(self, active=False):
        return self.send("Stop", stop_hook_active=active)

    def say(self, text):
        entry = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def state(self):
        return notebook.load({"session_id": self.sid, "cwd": str(self.project)})


# ---------------------------------------------------------------------------
# Notebook
# ---------------------------------------------------------------------------

class Notebook(TempProject):
    def payload(self):
        return {"session_id": self.sid, "cwd": str(self.project)}

    def test_save_creates_gitignore_and_loads_back(self):
        state = notebook.load(self.payload())
        notebook.record(state, {"kind": "user", "t": 1.0})
        notebook.save(self.payload(), state)
        folder = self.project / ".claude" / "loop_state"
        self.assertEqual((folder / ".gitignore").read_text(), "*\n")
        self.assertEqual(notebook.load(self.payload())["history"], [{"kind": "user", "t": 1.0}])
        self.assertEqual(len(state["project_id"]), 12)

    def test_save_is_atomic(self):
        notebook.save(self.payload(), notebook.load(self.payload()))
        path = notebook.state_path(self.payload())
        before = path.read_text()
        broken = notebook.load(self.payload())
        broken["bad"] = object()   # not JSON: the write fails halfway
        with self.assertRaises(TypeError):
            notebook.save(self.payload(), broken)
        self.assertEqual(path.read_text(), before)
        self.assertEqual([p.name for p in path.parent.glob(".tmp_*")], [])

    def test_unreadable_file_starts_fresh(self):
        notebook.ensure_state_dir(self.payload())
        notebook.state_path(self.payload()).write_text("{not json")
        self.assertEqual(notebook.load(self.payload())["history"], [])

    def test_history_is_capped_and_indexes_move(self):
        state = notebook.load(self.payload())
        state["episode"] = notebook.new_episode("npm test", None, 150)
        state["episode"]["last_nudge_idx"] = 190
        state["last_judge"] = 180
        for i in range(250):
            notebook.record(state, {"kind": "read", "t": float(i), "path": f"f{i}"})
        self.assertEqual(len(state["history"]), notebook.HISTORY_LIMIT)
        self.assertEqual(state["history"][0]["path"], "f50")
        self.assertEqual(state["episode"]["started_idx"], 100)
        self.assertEqual(state["episode"]["last_nudge_idx"], 140)
        self.assertEqual(state["last_judge"], 130)

    def test_piped_command_that_hides_a_failure(self):
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "pytest -q | tail -5"},
                   "tool_response": {"stdout": "FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed in 0.1s",
                                     "stderr": ""}}
        run, _ = notebook.action_from_tool_use(payload)
        self.assertFalse(run["ok"])
        self.assertIsNotNone(run["err_fp"])
        self.assertEqual(run["key"], "pytest")

    def test_piped_success_and_unpiped_commands_are_trusted(self):
        piped_ok = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "pytest | tail"},
                    "tool_response": {"stdout": "5 passed in 0.2s", "stderr": ""}}
        unpiped = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "cat log.txt"},
                   "tool_response": {"stdout": "Error: old failure in the log", "stderr": ""}}
        self.assertTrue(notebook.action_from_tool_use(piped_ok)[0]["ok"])
        self.assertTrue(notebook.action_from_tool_use(unpiped)[0]["ok"])

    def test_failure_payload_and_other_tools(self):
        run, output = notebook.action_from_tool_use({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                                                     "tool_input": {"command": "npm run build"}, "error": PRISMA})
        self.assertEqual((run["ok"], run["key"], run["err_fp"]), (False, "npm run build", struggles.fingerprint(PRISMA)))
        self.assertEqual(output, PRISMA)
        edit, _ = notebook.action_from_tool_use({"hook_event_name": "PostToolUse", "tool_name": "NotebookEdit",
                                                 "tool_input": {"notebook_path": "a.ipynb"}})
        self.assertEqual((edit["kind"], edit["path"]), ("edit", "a.ipynb"))
        read, _ = notebook.action_from_tool_use({"hook_event_name": "PostToolUse", "tool_name": "Grep",
                                                 "tool_input": {"pattern": "foo"}})
        self.assertEqual(read["kind"], "read")
        self.assertIsNone(notebook.action_from_tool_use({"tool_name": "TodoWrite", "tool_input": {}})[0])

    def test_project_id_without_git_is_a_hash_of_the_folder(self):
        self.assertEqual(notebook.project_id(self.project), notebook.project_id(self.project))
        self.assertRegex(notebook.project_id(self.project), r"^[0-9a-f]{12}$")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class Messages(unittest.TestCase):
    def test_first_line_and_facts(self):
        texts = [
            messages.known_fix(SOLVED, DC, "abc", ["x"]),
            messages.open_question_exists(OPEN, PT, "abc", []),
            messages.fix_failed(1, DC, "abc", ["x"]),
            messages.step_back(PT, "abc", [], "why"),
            messages.call_team(DC, PRISMA, "abc", ["x"], "why"),
            messages.call_team(DC, PRISMA, "abc", ["x"], "why", 3),
            messages.post_open_question(DC, "abc", ["x"], "why"),
            messages.notable_solve(DC, 3, "abc", []),
            messages.suspect_fix(DC, 3, "abc", ["added `|| true`"]),
            messages.not_done(DC, "abc", "why"),
        ]
        for text in texts:
            self.assertRegex(text.splitlines()[0], r"^\[Breakpoint: [A-Z_]+\] \S")
            self.assertIn(f"command: {DC if DC in text else PT}", text)
            self.assertIn("fingerprint: abc", text)
        self.assertIn("ask_for_help", texts[4])
        self.assertIn("check_help", texts[4])
        self.assertIn("  fingerprint: abc", texts[4])   # passed on to ask_for_help
        self.assertIn("files:", texts[4])
        self.assertIn("Open question #3", texts[5])
        self.assertIn(messages.COMMUNITY_LABEL.rstrip(), texts[0])
        self.assertIn(messages.COMMUNITY_LABEL.rstrip(), texts[1])
        self.assertIn("already tried: x", texts[0])

    def test_output_shapes(self):
        self.assertEqual(messages.context("Stop", "t"), {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "t"}})
        self.assertEqual(messages.block("t"), {"decision": "block", "reason": "t"})
        self.assertEqual(messages.REWIND_OFFER, "REWIND_OFFER")


# ---------------------------------------------------------------------------
# Brain: the help ladder with a fake backend
# ---------------------------------------------------------------------------

def fail_then_forum(case, result, cmd=DC, err=PRISMA):
    """Two failures while the forum has nothing (the early check finds no match), then the forum gets `result`
    before the third failure, where the ladder's own search runs. Returns the third failure's output."""
    case.backend.search_result = []
    case.assertIsNone(case.fail(cmd, err))
    case.edit("Dockerfile")
    case.assertIsNone(case.fail(cmd, err))
    case.backend.search_result = result
    return case.fail(cmd, err)


class Ladder(TempProject):
    def test_known_fix_then_outcome_worked(self):
        out = fail_then_forum(self, [SOLVED])
        self.assertEqual(tag(out), "KNOWN_FIX")
        self.assertEqual(self.backend.calls.count(("search", DC, struggles.fingerprint(PRISMA))), 2)   # early + rung 1
        self.assertEqual(self.state()["episode"]["pending_fix"],
                         {"post_id": 1, "key": "docker compose up", "fp": struggles.fingerprint(PRISMA)})
        self.edit("prisma/schema.prisma")
        out = self.ok(DC, "listening on :3000")
        self.assertIn(("outcome", 1, True), self.backend.calls)
        self.assertEqual(tag(out), "LEAVE_REPLY")   # a posted fix worked: a reply, never NOTABLE_SOLVE
        self.assertTrue(text_of(out).splitlines()[0].endswith("The fix from post #1 worked."))
        self.assertIn(f"command: {DC}", text_of(out))
        self.assertIn("post: #1", text_of(out))
        self.assertIn("`reply` tool (post 1)", text_of(out))
        self.assertIsNone(self.state()["episode"])
        self.assertEqual(self.state()["reply_nudged"], [1])
        self.assertIsNone(self.stop())   # never at Stop

    def test_leave_reply_at_most_once_per_post_per_session(self):
        self.backend.search_result = [SOLVED]
        self.assertEqual(tag(self.fail(DC, PRISMA)), "KNOWN_FIX")   # the early check
        self.assertEqual(tag(self.ok(DC)), "LEAVE_REPLY")
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "KNOWN_FIX")   # the same post again, later in the session (the ladder's search)
        self.assertIsNone(self.ok(DC))            # outcome reported, but no second LEAVE_REPLY
        self.assertEqual(self.backend.calls.count(("outcome", 1, True)), 2)

    def test_known_fix_from_ask_for_help_also_leaves_a_reply(self):
        self.fail(DC, PRISMA)
        self.mcp("ask_for_help", "KNOWN_FIX: forum post #4 matches this problem (same error fingerprint).")
        out = self.ok(DC)
        self.assertEqual(tag(out), "LEAVE_REPLY")
        self.assertIn("post: #4", text_of(out))
        self.assertIn(("outcome", 4, True), self.backend.calls)

    def test_no_leave_reply_when_the_agent_already_replied(self):
        self.backend.search_result = [SOLVED]
        self.assertEqual(tag(self.fail(DC, PRISMA)), "KNOWN_FIX")
        self.send("PostToolUse", tool_name="mcp__plugin_breakpoint_breakpoint__reply",
                  tool_input={"post_id": 1, "body": "Also needed openssl on Alpine 3.20."},
                  tool_response=[{"type": "text", "text": "Reply #12 | agent-7f3a (X) | now | Upvotes: 0\n..."}])
        self.assertIsNone(self.ok(DC))
        self.assertIn(("outcome", 1, True), self.backend.calls)

    def test_fix_failed_reports_and_moves_on(self):
        out = fail_then_forum(self, [SOLVED])
        self.assertEqual(tag(out), "KNOWN_FIX")
        self.edit("Dockerfile")
        out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "FIX_FAILED")
        self.assertIn(("outcome", 1, False), self.backend.calls)
        self.assertIn("[Breakpoint: STUCK_MENU]", text_of(out))   # node_modules in the trace: external
        self.assertIn("forum fix from post #1 -> did not work", self.state()["episode"]["tried"])

    def test_no_match_external_calls_the_team(self):
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "STUCK_MENU")   # team_mode ask: the user picks (StuckMenu below)
        self.assertIn("AskUserQuestion", text_of(out))
        self.assertIn("already tried:", text_of(out))

    def test_backend_down_still_climbs(self):
        self.backend.search_result = None
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "STUCK_MENU")
        self.assertIn("Call the specialist team", text_of(out))

    def test_open_question_then_team_next_time(self):
        out = fail_then_forum(self, [OPEN])
        self.assertEqual(tag(out), "OPEN_QUESTION_EXISTS")
        self.assertIn("#3", text_of(out))
        self.edit()
        self.assertIsNone(self.fail(DC, PRISMA))   # cooldown
        out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "STUCK_MENU")

    def test_weak_text_match_is_not_a_known_fix(self):
        self.backend.search_result = [{**SOLVED, "match": "text", "score": 0.3}]
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "STUCK_MENU")

    def test_own_code_steps_back_then_calls_team(self):
        for _ in range(3):
            out = self.fail(PT, PYTEST)
        self.assertEqual(tag(out), "STEP_BACK")
        self.edit("cart.py")
        self.assertIsNone(self.fail(PT, PYTEST))   # cooldown: only 2 actions since the nudge
        self.edit("cart.py")
        self.assertEqual(tag(self.fail(PT, PYTEST)), "STUCK_MENU")

    def test_team_ran_and_still_stuck_posts_open_question(self):
        for _ in range(3):
            self.fail(DC, PRISMA)
        self.assertIsNone(self.mcp("ask_for_help", messages.COMMUNITY_LABEL + "TEAM_STARTED: r1\nThe team is on it."))
        self.assertTrue(self.state()["episode"]["team_ran"])
        self.assertEqual(self.state()["episode"]["team_run_id"], "r1")
        self.edit()
        self.assertIsNone(self.fail(DC, PRISMA))
        self.assertIsNone(self.fail(DC, PRISMA))   # the team is still working: no Open Question yet
        self.assertEqual(tag(self.mcp("check_help", "TEAM_NO_ANSWER: r1\nNothing found.")), "POST_OPEN_QUESTION")
        self.mcp("post_open_question", "Posted as new open question #9.")
        self.assertTrue(self.state()["episode"]["oq_posted"])

    def test_judge_actions_carry_both_shapes(self):
        self.fail("npm test", "Exit code 1\nError: boom")
        self.edit("a.js")
        actions = brain.recent_actions(self.state()["history"])
        self.assertEqual({k: actions[0][k] for k in ("tool", "summary", "ok", "kind", "cmd")},
                         {"tool": "Bash", "summary": "npm test", "ok": False, "kind": "run", "cmd": "npm test"})
        self.assertEqual((actions[1]["tool"], actions[1]["path"], actions[1]["ok"]), ("Edit", "a.js", True))

    def test_notable_solve(self):
        for _ in range(3):
            self.fail(DC, PRISMA)
        self.edit("Dockerfile")
        out = self.ok(DC, "listening")
        self.assertEqual(tag(out), "NOTABLE_SOLVE")
        self.assertIn("after failing 3 times", text_of(out))
        self.assertIsNone(self.state()["episode"])

    def test_quick_fix_is_not_notable(self):
        self.fail(DC, PRISMA)
        self.edit()
        self.assertIsNone(self.ok(DC))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_suspect_fix_from_git_diff(self):
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
        tests = self.project / "tests"
        tests.mkdir()
        (tests / "test_cart.py").write_text("def test_total():\n    assert 1\n\ndef test_other():\n    assert 1\n")
        subprocess.run(git + ["init", "-q"], cwd=self.project, check=True)
        subprocess.run(git + ["add", "tests"], cwd=self.project, check=True)
        subprocess.run(git + ["commit", "-qm", "init"], cwd=self.project, check=True)
        for _ in range(3):
            self.fail(PT, PYTEST)
        (tests / "test_cart.py").write_text("import pytest\n\n@pytest.mark.skip\ndef test_total():\n    assert 1\n")
        self.edit("tests/test_cart.py")
        out = self.ok(PT, "1 passed, 1 skipped")
        self.assertEqual(tag(out), "SUSPECT_FIX")
        self.assertIn("@pytest.mark.skip", text_of(out))
        self.assertIn("removed test `test_other`", text_of(out))

    def test_user_pushback_runs_the_ladder(self):
        self.fail("npm test", "Exit code 1\nError: boom")
        out = self.user("it's still broken")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(tag(out), "STUCK_MENU")
        self.assertIn("command: npm test", text_of(out))

    def test_ai_judge_counts_as_a_strong_signal(self):
        self.backend.judge_result = {"stuck": True, "reason": "edits keep circling", "confidence": 0.9}
        self.fail("npm test", "Exit code 1\nError: boom")
        for i in range(20):
            self.send("PostToolUse", tool_name="Read", tool_input={"file_path": f"f{i}.js"}, tool_response={})
        out = self.fail("npm test", "Exit code 1\nError: boom")
        self.assertTrue(any(c[0] == "judge" for c in self.backend.calls))
        self.assertIn("AI judge: edits keep circling", text_of(out))

    def test_unsure_judge_is_ignored(self):
        self.backend.judge_result = {"stuck": True, "reason": "maybe", "confidence": 0.5}
        self.fail("npm test", "Exit code 1\nError: boom")
        for i in range(20):
            self.send("PostToolUse", tool_name="Read", tool_input={"file_path": f"f{i}.js"}, tool_response={})
        self.assertIsNone(self.fail("npm test", "Exit code 1\nError: boom"))

    def test_session_start_only_syncs_identity_and_shows_nothing(self):
        """No more session-start "updates": news about solved questions goes out by email (docs/accounts.md)."""
        self.assertIsNone(self.send("SessionStart", source="startup"))
        self.assertEqual(self.backend.calls, [("sync", "startup")])
        self.assertIsNone(self.send("SessionStart", source="compact"))   # same session continuing: nothing
        self.assertEqual(self.backend.calls, [("sync", "startup")])
        self.assertFalse(hasattr(backend, "get_updates"))
        self.assertFalse(hasattr(messages, "UPDATES"))

    def test_known_fix_shows_top_replies_as_community_notes(self):
        replies = [{"author": "agent-7f3a (Claude Sonnet 5.5)", "body": "Also add openssl to the image.\nThen rebuild.",
                    "upvotes": 3},
                   {"author": "agent-1b2c (GPT)", "body": "word " * 100, "upvotes": 1},
                   {"author": "agent-9999 (X)", "body": "third one is not shown", "upvotes": 0}]
        text = messages.known_fix({**SOLVED, "top_replies": replies}, DC, "abc", [])
        lines = text.splitlines()
        notes = lines.index("Community notes (top replies from other agents who used this fix):")
        self.assertGreater(notes, next(i for i, ln in enumerate(lines) if ln.startswith("Solution:")))
        self.assertGreater(notes, lines.index(messages.COMMUNITY_LABEL.rstrip().splitlines()[-1]))   # labeled
        self.assertEqual(lines[notes + 1],
                         "- agent-7f3a (Claude Sonnet 5.5), 3 upvotes: Also add openssl to the image. Then rebuild.")
        self.assertTrue(lines[notes + 2].startswith("- agent-1b2c (GPT), 1 upvote: word word"))
        self.assertTrue(lines[notes + 2].endswith("...") and len(lines[notes + 2]) < 260)
        self.assertNotIn("third one", text)
        self.assertNotIn("Community notes", messages.known_fix(SOLVED, DC, "abc", []))
        self.assertNotIn("Community notes", messages.known_fix({**SOLVED, "top_replies": "junk"}, DC, "abc", []))

    def test_known_fix_shows_the_useful_count(self):
        """The real tables keep "useful" marks, not times worked/applied."""
        text = messages.known_fix(SOLVED, DC, "abc", [])
        self.assertTrue(text.startswith("[Breakpoint: KNOWN_FIX]"))
        self.assertIn("(marked useful by 9 agents)", text.splitlines()[0])
        self.assertIn("marked useful by 1 agent", messages._proof({"useful_count": 1}))
        self.assertIn("worked 9 of 10 times", messages._proof({"times_worked": 9, "times_applied": 10}))

    def test_probe_commands_never_struggle(self):
        for _ in range(5):
            self.assertIsNone(self.fail("grep -r foo .", "Exit code 1"))
        self.assertEqual(self.backend.calls, [])


# ---------------------------------------------------------------------------
# LEAVE_REPLY after a solved post the agent found itself (search / get_post)
# ---------------------------------------------------------------------------

def solved_header(post_id, solved=True):
    label = "SOLVED" if solved else "OPEN QUESTION"
    return f"{label} post #{post_id} | forum: docker | tags: docker | votes: 2 | Visited: 1 | Useful: 0 | Replies: 0"


class ViewedPosts(TempProject):
    def search_tool(self, *headers):
        text = messages.COMMUNITY_LABEL + "\n\n=====\n\n".join(h + "\n\nProblem:\nx\n\nGeneral solution:\ny"
                                                                for h in headers)
        return self.send("PostToolUse", tool_name="mcp__plugin_breakpoint_breakpoint__search",
                         tool_input={"error": "x"}, tool_response=[{"type": "text", "text": text}])

    def get_post_tool(self, post_id, solved=True, replies="No replies yet."):
        text = (messages.COMMUNITY_LABEL + solved_header(post_id, solved) + "\n\nProblem:\nx\n\nAgent replies:\n"
                + replies)
        return self.send("PostToolUse", tool_name="mcp__plugin_breakpoint_breakpoint__get_post",
                         tool_input={"post_id": post_id}, tool_response=[{"type": "text", "text": text}])

    def test_search_then_the_failing_command_passes(self):
        self.fail(DC, PRISMA)
        self.assertIsNone(self.search_tool(solved_header(3, solved=False), solved_header(4), solved_header(5)))
        self.assertEqual(self.state()["viewed_posts"],
                         [{"post_id": 4, "key": "docker compose up", "fp": struggles.fingerprint(PRISMA)}])
        self.edit("Dockerfile")
        out = self.ok(DC)
        self.assertEqual(tag(out), "LEAVE_REPLY")
        self.assertIn("after you read the fix in post #4", text_of(out).splitlines()[0])
        self.assertIn("post: #4", text_of(out))
        self.assertIn("If you did not use post #4, skip this.", text_of(out))
        self.assertIn(("outcome", 4, True), self.backend.calls)
        self.assertEqual(self.state()["viewed_posts"], [])
        self.assertIsNone(self.stop())

    def test_get_post_uses_the_header_not_the_replies(self):
        self.fail(DC, PRISMA)
        self.get_post_tool(7, replies="Reply #1 | a | t | Upvotes: 0\n" + solved_header(99))
        self.assertEqual(tag(self.ok(DC)), "LEAVE_REPLY")
        self.assertIn(("outcome", 7, True), self.backend.calls)
        self.assertNotIn(("outcome", 99, True), self.backend.calls)

    def test_the_most_recent_viewed_post_counts(self):
        self.fail(DC, PRISMA)
        self.get_post_tool(7)
        self.search_tool(solved_header(8))
        out = self.ok(DC)
        self.assertIn("post: #8", text_of(out))
        self.assertEqual([c for c in self.backend.calls if c[0] == "outcome"], [("outcome", 8, True)])

    def test_known_fix_post_wins_over_viewed_posts(self):
        self.backend.search_result = [SOLVED]
        self.assertEqual(tag(self.fail(DC, PRISMA)), "KNOWN_FIX")
        self.get_post_tool(8)
        out = self.ok(DC)
        self.assertIn("post: #1", text_of(out))
        self.assertEqual([c for c in self.backend.calls if c[0] == "outcome"], [("outcome", 1, True)])
        self.assertEqual(self.state()["viewed_posts"], [])

    def test_a_known_fix_that_failed_is_not_credited_later(self):
        self.backend.search_result = [SOLVED]
        self.assertEqual(tag(self.fail(DC, PRISMA)), "KNOWN_FIX")
        self.get_post_tool(1)   # the agent reads the KNOWN_FIX post in full, as the skill says
        self.assertEqual(tag(self.fail(DC, PRISMA)), "FIX_FAILED")
        self.edit()
        self.assertNotEqual(tag(self.ok(DC)), "LEAVE_REPLY")
        self.assertNotIn(("outcome", 1, True), self.backend.calls)

    def test_open_questions_and_nothing_failing_do_not_count(self):
        self.fail(DC, PRISMA)
        self.get_post_tool(3, solved=False)
        self.search_tool(solved_header(3, solved=False))
        self.assertEqual(self.state()["viewed_posts"], [])
        self.ok(DC)
        self.get_post_tool(4)   # nothing is failing now
        self.assertEqual(self.state()["viewed_posts"], [])
        self.assertIsNone(self.ok(DC))
        self.assertFalse(any(c[0] == "outcome" for c in self.backend.calls))

    def test_once_per_post_per_session(self):
        for _ in range(2):
            self.fail(DC, PRISMA)
            self.get_post_tool(4)
            out = self.ok(DC)
        self.assertIsNone(out)
        self.assertEqual(self.backend.calls.count(("outcome", 4, True)), 2)

    def test_tester_winner_gets_notable_solve_not_a_reply(self):
        for _ in range(3):
            self.fail(DC, PRISMA)
        self.mcp("ask_for_help", "TEAM_STARTED: run-1")
        self.mcp("check_help", "TEAM_REPORT: run-1\n1. ...")
        self.get_post_tool(4)
        tester = messages.tester_command(self.project, self.sid, "run-1", DC)
        self.send("PostToolUse", tool_name="Bash", tool_input={"command": tester},
                  tool_response={"stdout": "WINNER: hypothesis 1 (musl engine)\n", "stderr": ""})
        self.edit("schema.prisma")
        self.assertEqual(tag(self.ok(DC)), "NOTABLE_SOLVE")
        self.assertFalse(any(c[0] == "outcome" for c in self.backend.calls))

    def test_older_notes_files_get_the_new_lists(self):
        folder = self.project / ".claude" / "loop_state"
        folder.mkdir(parents=True)
        (folder / f"{self.sid}.json").write_text(json.dumps({"history": [], "project_id": "p"}))
        state = self.state()
        self.assertEqual((state["viewed_posts"], state["reply_nudged"]), ([], []))


# ---------------------------------------------------------------------------
# The PowerShell tool, and runs whose result is unknown (background, interrupted)
# ---------------------------------------------------------------------------

class ShellTools(TempProject):
    def ps_fail(self, cmd, err):
        return self.send("PostToolUseFailure", tool_name="PowerShell", tool_input={"command": cmd}, error=err)

    def ps_ok(self, cmd, out="ok", **response):
        return self.send("PostToolUse", tool_name="PowerShell", tool_input={"command": cmd},
                         tool_response={"stdout": out, "stderr": "", "interrupted": False, **response})

    def test_powershell_is_a_run_like_bash(self):
        run, output = notebook.action_from_tool_use({"hook_event_name": "PostToolUseFailure", "tool_name": "PowerShell",
                                                     "tool_input": {"command": "npm run build"}, "error": PRISMA})
        self.assertEqual((run["kind"], run["ok"], run["key"], run["err_fp"]),
                         ("run", False, "npm run build", struggles.fingerprint(PRISMA)))
        self.assertEqual(output, PRISMA)
        for response, ok in [({"stdout": "done"}, True), ({"stdout": "x", "exitCode": 1}, False),
                             ({"stdout": "x", "exit_code": "2"}, False), ({"stdout": "x", "returnCode": "0"}, True),
                             ({"stdout": "x", "exitCode": None}, True),
                             ({"stdout": "FAILED tests/test_a.py::test_x\n1 failed in 0.1s"}, True)]:
            payload = {"hook_event_name": "PostToolUse", "tool_name": "PowerShell",
                       "tool_input": {"command": "pytest -q"}, "tool_response": response}
            self.assertEqual(notebook.action_from_tool_use(payload)[0]["ok"], ok, response)
        piped = {"hook_event_name": "PostToolUse", "tool_name": "PowerShell",
                 "tool_input": {"command": "pytest -q | Select-Object -Last 5"},
                 "tool_response": {"stdout": "FAILED tests/test_a.py::test_x\n1 failed in 0.1s"}}
        self.assertFalse(notebook.action_from_tool_use(piped)[0]["ok"])   # the same piped-output check as Bash

    def test_powershell_known_fix_round_trip(self):
        self.backend.search_result = [SOLVED]
        self.assertEqual(tag(self.ps_fail(DC, PRISMA)), "KNOWN_FIX")   # the early check, on the first failure
        self.edit("Dockerfile")
        self.assertEqual(tag(self.ps_ok(DC, "listening")), "LEAVE_REPLY")
        self.assertIn(("outcome", 1, True), self.backend.calls)

    def test_hooks_json_sends_powershell_and_only_our_mcp_tools(self):
        config = json.loads((HOOK.parent / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        for event in ("PostToolUse", "PostToolUseFailure"):
            matcher = re.compile(config["hooks"][event][0]["matcher"])
            for name in ("Bash", "PowerShell", "Edit", "Read", "mcp__plugin_breakpoint_breakpoint__post",
                         "mcp__breakpoint__ask_for_help"):
                self.assertTrue(matcher.search(name), (event, name))
            for name in ("TodoWrite", "BashOutput", "mcp__github__post", "mcp__plugin_other_other__search"):
                self.assertFalse(matcher.search(name), (event, name))

    def test_background_and_interrupted_runs_are_unknown(self):
        unknown = [
            {"hook_event_name": "PostToolUse", "tool_name": "Bash",
             "tool_input": {"command": DC, "run_in_background": True}, "tool_response": {"stdout": ""}},
            {"hook_event_name": "PostToolUse", "tool_name": "PowerShell",
             "tool_input": {"command": DC}, "tool_response": {"stdout": "", "backgroundTaskId": "b1"}},
            {"hook_event_name": "PostToolUse", "tool_name": "Bash",
             "tool_input": {"command": DC}, "tool_response": {"stdout": "starting", "interrupted": True}},
            {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
             "tool_input": {"command": DC}, "error": "Interrupted by user", "is_interrupt": True},
            {"hook_event_name": "PostToolUseFailure", "tool_name": "PowerShell",
             "tool_input": {"command": DC, "run_in_background": True}, "error": "Exit code 1"},
        ]
        for payload in unknown:
            self.assertEqual(notebook.action_from_tool_use(payload), (None, ""), payload)

    def test_background_run_does_not_confirm_a_known_fix(self):
        self.backend.search_result = [SOLVED]
        self.assertEqual(tag(self.fail(DC, PRISMA)), "KNOWN_FIX")
        self.edit("Dockerfile")
        self.assertIsNone(self.send("PostToolUse", tool_name="Bash",
                                    tool_input={"command": DC, "run_in_background": True},
                                    tool_response={"stdout": "", "stderr": "", "backgroundTaskId": "b1"}))
        self.assertIsNone(self.send("PostToolUseFailure", tool_name="Bash", tool_input={"command": DC},
                                    error="Interrupted by user", is_interrupt=True))
        self.assertFalse(any(c[0] == "outcome" for c in self.backend.calls))   # neither a pass nor a fail
        self.assertEqual(self.state()["episode"]["pending_fix"]["post_id"], 1)   # still waiting for a real run
        self.assertEqual(tag(self.ok(DC)), "LEAVE_REPLY")
        self.assertEqual([c for c in self.backend.calls if c[0] == "outcome"], [("outcome", 1, True)])


# ---------------------------------------------------------------------------
# Our MCP tools: exact names, and post / post_open_question only count when saved
# ---------------------------------------------------------------------------

class PostResults(TempProject):
    def stuck(self):
        for _ in range(3):
            self.fail(DC, PRISMA)
        return self.state()["episode"]

    def test_open_question_counts_only_when_saved(self):
        self.stuck()
        for reply in ("REVISE: The post was not saved yet (try 1 of at most 3). Fix: ...",
                      "Could not reach the forum: connection refused", "Rejected: unsafe content"):
            self.mcp("post_open_question", reply)
            self.assertFalse(self.state()["episode"]["oq_posted"], reply)
        self.mcp("post_open_question", "Posted as new open question #9. Filed under docker.")
        self.assertEqual((self.state()["episode"]["oq_posted"], self.state()["episode"]["open_question_id"]), (True, 9))

    def test_duplicate_open_question_counts_as_posted(self):
        self.stuck()
        self.mcp("post_open_question", "Already on the forum as open question #5; nothing new posted.")
        self.assertEqual((self.state()["episode"]["oq_posted"], self.state()["episode"]["open_question_id"]), (True, 5))

    def test_post_closes_the_episode_only_when_saved(self):
        self.stuck()
        for reply in ("REVISE: The post was not saved yet (try 1 of at most 3).", "Forum error 500: boom"):
            self.mcp("post", reply)
            self.assertIsNotNone(self.state()["episode"], reply)
        self.mcp("post", "Solution attached to open question #3.")
        self.assertIsNone(self.state()["episode"])
        self.stuck()
        self.mcp("post", "Posted as new solved post #12. Filed under docker.")
        self.assertIsNone(self.state()["episode"])

    def test_other_servers_tools_are_ignored(self):
        self.stuck()
        for name in ("mcp__github__post", "mcp__plugin_other_other__post_open_question"):
            self.send("PostToolUse", tool_name=name, tool_input={},
                      tool_response=[{"type": "text", "text": "Posted as new open question #9."}])
        self.assertFalse(self.state()["episode"]["oq_posted"])
        self.send("PostToolUse", tool_name="mcp__breakpoint__post", tool_input={},   # the server added by hand
                  tool_response=[{"type": "text", "text": "Posted as new solved post #12."}])
        self.assertIsNone(self.state()["episode"])

    def test_notable_solve_names_the_open_question(self):
        out = fail_then_forum(self, [OPEN])
        self.assertEqual(tag(out), "OPEN_QUESTION_EXISTS")
        self.edit("Dockerfile")
        out = self.ok(DC, "listening")
        self.assertEqual(tag(out), "NOTABLE_SOLVE")
        self.assertIn("pass open_question_id 3 to `post`", text_of(out))
        self.assertNotIn("open_question_id", messages.notable_solve(DC, 3, "abc", []))


# ---------------------------------------------------------------------------
# The early forum check: one search on the first failure of each new error fingerprint
# ---------------------------------------------------------------------------

class EarlyLookup(TempProject):
    def searches(self):
        return [c for c in self.backend.calls if c[0] == "search"]

    def test_solved_match_on_the_first_failure(self):
        self.backend.search_result = [SOLVED]
        out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "KNOWN_FIX")
        self.assertIn("post: #1", text_of(out))
        self.assertEqual(self.searches(), [("search", DC, struggles.fingerprint(PRISMA))])
        episode = self.state()["episode"]
        self.assertEqual((episode["rung"], episode["early"], episode["key"]), (1, True, "docker compose up"))
        self.assertEqual(episode["pending_fix"],
                         {"post_id": 1, "key": "docker compose up", "fp": struggles.fingerprint(PRISMA)})
        self.edit("Dockerfile")
        out = self.ok(DC, "listening on :3000")
        self.assertIn(("outcome", 1, True), self.backend.calls)
        self.assertEqual(tag(out), "LEAVE_REPLY")
        self.assertIn("The fix from post #1 worked.", text_of(out))
        self.assertIsNone(self.state()["episode"])
        self.assertIsNone(self.stop())

    def test_open_match_on_the_first_failure_then_notable_solve(self):
        self.backend.search_result = [OPEN]
        out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "OPEN_QUESTION_EXISTS")
        self.assertIn("open question #3", text_of(out))
        self.assertEqual((self.state()["episode"]["open_question_id"], self.state()["episode"]["rung"]), (3, 1))
        self.edit("Dockerfile")
        out = self.ok(DC, "listening")
        self.assertEqual(tag(out), "NOTABLE_SOLVE")   # even after a single failure: it answers an open question
        self.assertIn("after failing 1 time.", text_of(out).splitlines()[0])
        self.assertIn("Open question #3", text_of(out))
        self.assertIn("pass open_question_id 3 to `post`", text_of(out))
        self.assertIsNone(self.state()["episode"])

    def test_open_match_then_two_failures_still_names_the_question(self):
        self.backend.search_result = [OPEN]
        self.assertEqual(tag(self.fail(DC, PRISMA)), "OPEN_QUESTION_EXISTS")
        self.edit()
        self.assertIsNone(self.fail(DC, PRISMA))   # same error: already looked up, and no struggle yet
        self.edit()
        out = self.ok(DC)
        self.assertEqual(tag(out), "NOTABLE_SOLVE")
        self.assertIn("after failing 2 times", text_of(out))
        self.assertIn("pass open_question_id 3 to `post`", text_of(out))
        self.assertEqual(len(self.searches()), 1)

    def test_no_match_stays_silent(self):
        self.backend.search_result = [{**SOLVED, "match": "text", "score": 0.3}]   # a weak match counts as none
        self.assertIsNone(self.fail(DC, PRISMA))
        self.assertEqual(len(self.searches()), 1)
        self.assertIsNone(self.state()["episode"])
        self.edit()
        self.assertIsNone(self.ok(DC))   # fixed quickly: nothing to say, as before

    def test_the_same_fingerprint_is_looked_up_once(self):
        self.assertIsNone(self.fail(DC, PRISMA))
        self.edit()
        self.assertIsNone(self.fail(DC, PRISMA))
        self.assertEqual(len(self.searches()), 1)
        self.ok(DC)
        self.assertIsNone(self.fail(DC, PRISMA))   # later in the session, after a pass: still not again
        self.assertEqual(len(self.searches()), 1)
        other = "Exit code 1\nError: Cannot find module 'express'"
        self.assertIsNone(self.fail(DC, other))    # a new error: looked up once
        self.assertEqual(len(self.searches()), 2)
        self.assertEqual(self.state()["looked_up"], [struggles.fingerprint(PRISMA), struggles.fingerprint(other)])

    def test_server_down_stays_silent(self):
        self.backend.search_result = None
        self.assertIsNone(self.fail(DC, PRISMA))
        self.assertIsNone(self.state()["episode"])

    def test_server_down_through_the_real_backend(self):
        """backend.py's down-skip: the first call finds the port closed, the next ones skip the network."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{s.getsockname()[1]}"
        os.environ["BREAKPOINT_API_URL"] = url
        self.addCleanup(backend._mark_down, url, False)
        patcher = mock.patch.object(backend, "CONFIG_PATH", self.project / "no_config.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = backend
        self.assertIsNone(self.fail(DC, PRISMA))
        started = time.time()
        self.assertIsNone(self.fail(DC, "Exit code 1\nError: Cannot find module 'express'"))   # new error, skipped fast
        self.assertLess(time.time() - started, 1.0)
        self.assertIsNone(self.state()["episode"])

    def test_a_failed_early_fix_waits_for_the_ladder(self):
        self.backend.search_result = [SOLVED]
        self.assertEqual(tag(self.fail(DC, PRISMA)), "KNOWN_FIX")
        self.edit("Dockerfile")
        out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "FIX_FAILED")
        self.assertNotIn("STUCK_MENU", text_of(out))   # 2 failures: not stuck yet, so no next step
        self.assertIn(("outcome", 1, False), self.backend.calls)
        self.edit()
        self.assertIsNone(self.fail(DC, PRISMA))      # cooldown
        self.edit()
        self.assertEqual(tag(self.fail(DC, PRISMA)), "STUCK_MENU")   # stuck now; the forum step is already done
        self.assertEqual(len(self.searches()), 1)

    def test_not_while_an_episode_is_about_another_command(self):
        for _ in range(3):
            out = self.fail(PT, PYTEST)
        self.assertEqual(tag(out), "STEP_BACK")
        self.backend.search_result = [SOLVED]
        self.assertIsNone(self.fail(DC, PRISMA))
        self.assertEqual(self.state()["episode"]["key"], struggles.command_key(PT))


class Triage(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(brain.classify(PRISMA)[0], "external")
        self.assertEqual(brain.classify(PYTEST)[0], "own_code")
        self.assertEqual(brain.classify("bash: foo: command not found")[0], "external")
        self.assertEqual(brain.classify('Traceback\n  File "src/app.py", line 3\nKeyError: x')[0], "own_code")
        self.assertEqual(brain.classify("something odd happened")[0], "external")


class Stop(TempProject):
    def test_not_done_blocks_once(self):
        self.user("fix the tests")
        self.fail("npm test", "Exit code 1\nTests: 1 failed")
        self.say("All done!")
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertEqual(tag(out), "NOT_DONE")
        self.assertIsNone(self.stop())   # once per episode

    def test_never_blocks_when_stop_hook_active(self):
        self.user("fix the tests")
        self.fail("npm test", "Exit code 1\nTests: 1 failed")
        self.assertIsNone(self.stop(active=True))

    def test_unverified_claim(self):
        self.user("fix the build")
        self.fail("npm run build", "Exit code 1\nError: x")
        self.edit("src/a.ts")
        self.ok("npm run build", "built")
        self.edit("src/b.ts")
        self.say("I've fixed the issue, it should work now.")
        self.assertEqual(tag(self.stop()), "UNVERIFIED_CLAIM")

    def test_team_ran_and_unsolved_asks_for_open_question(self):
        for _ in range(3):
            self.fail(DC, PRISMA)
        self.mcp("ask_for_help", "TEAM_STARTED: r1")
        self.mcp("check_help", "TEAM_REPORT: r1\n1. Alpine needs the musl engine ...")
        self.user("ok, what now?")
        self.assertEqual(tag(self.stop()), "POST_OPEN_QUESTION")

    def test_clean_finish_and_missing_transcript(self):
        self.transcript = self.project / "does_not_exist.jsonl"
        self.user("add a readme")
        self.edit("README.md")
        self.assertIsNone(self.stop())

    def test_transcript_reader_is_defensive(self):
        lines = ["not json", json.dumps({"type": "user", "message": {"content": "hi"}}),
                 json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}}),
                 json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}}),
                 json.dumps({"type": "assistant", "message": "weird"}),
                 json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "last"}]}})]
        self.transcript.write_text("\n".join(lines), encoding="utf-8")
        self.assertEqual(brain.assistant_replies(str(self.transcript), 5), ["first", "last"])
        self.assertEqual(brain.assistant_replies(None, 5), [])


# ---------------------------------------------------------------------------
# Backend: the HTTP contract, against a tiny local server
# ---------------------------------------------------------------------------

class FakeServer:
    """Answers from `routes` {(method, path): (status, json_or_bytes)} and records every request."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self, method):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"method": method, "path": url.path, "query": parse_qs(url.query),
                                       "key": self.headers.get("X-API-Key"),
                                       "body": json.loads(body) if body else None})
                status, reply = outer.routes.get((method, url.path), (404, {"detail": "not found"}))
                data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Backend(TempProject):
    def setUp(self):
        super().setUp()
        self.server = FakeServer({
            ("GET", "/api/search"): (200, [SOLVED]),
            ("POST", "/api/outcomes"): (200, {"post_status": "solved"}),
            ("POST", "/api/judge"): (200, {"stuck": True, "reason": "r", "confidence": 0.8}),
        })
        self.addCleanup(self.server.close)
        os.environ["BREAKPOINT_API_URL"] = self.server.url
        os.environ["BREAKPOINT_API_KEY"] = "k1"
        patcher = mock.patch.object(backend, "CONFIG_PATH", self.project / "no_config.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_search_contract(self):
        self.assertEqual(backend.search("E" * 5000, "npm test", "abcd1234", ["node", "esm"]), [SOLVED])
        req = self.server.requests[-1]
        self.assertEqual((req["method"], req["path"], req["key"]), ("GET", "/api/search", "k1"))
        self.assertEqual(len(req["query"]["error"][0]), 2000)
        self.assertEqual(req["query"]["command"], ["npm test"])
        self.assertEqual(req["query"]["fingerprint"], ["abcd1234"])
        self.assertEqual(req["query"]["tags"], ["node,esm"])

    def test_outcome_and_judge_contract(self):
        self.assertEqual(backend.report_outcome(1, True, "fp", "npm test", "s1"), {"post_status": "solved"})
        self.assertEqual(self.server.requests[-1]["body"], {"post_id": 1, "worked": True, "fingerprint": "fp",
                                                            "command": "npm test", "session_id": "s1", "source": "hook"})
        actions = [{"tool": "Bash", "summary": "npm test", "ok": False, "out": "token=abc123"}]
        self.assertEqual(backend.judge("s1", actions, ["reply"])["stuck"], True)
        sent = self.server.requests[-1]["body"]
        self.assertEqual(sent["session_id"], "s1")
        self.assertEqual(sent["replies"], ["reply"])
        self.assertEqual(sent["actions"][0]["out"], "token=<secret>")   # scrubbed

    def test_errors_return_none(self):
        self.server.routes[("GET", "/api/search")] = (500, {"detail": "boom"})
        self.assertIsNone(backend.search("e", "c", "f"))
        self.server.routes[("GET", "/api/search")] = (200, b"not json")
        self.assertIsNone(backend.search("e", "c", "f"))
        self.server.routes[("GET", "/api/search")] = (200, {"not": "a list"})
        self.assertIsNone(backend.search("e", "c", "f"))
        self.server.routes[("POST", "/api/judge")] = (200, {"no": "stuck field"})
        self.assertIsNone(backend.judge("s", [], []))

    def test_server_down_returns_none_and_is_remembered(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        os.environ["BREAKPOINT_API_URL"] = f"http://127.0.0.1:{port}"
        self.addCleanup(backend._mark_down, f"http://127.0.0.1:{port}", False)
        self.assertIsNone(backend.search("e", "c", "f"))
        started = time.time()
        self.assertIsNone(backend.report_outcome(1, True, "f", "c", "s"))
        self.assertLess(time.time() - started, 0.5)

    def test_config_order(self):
        config = self.project / "config.json"
        config.write_text(json.dumps({"api_url": "http://example.test:1", "api_key": "file"}))
        with mock.patch.object(backend, "CONFIG_PATH", config):
            self.assertEqual(backend.api_url(), self.server.url)    # env wins
            del os.environ["BREAKPOINT_API_URL"], os.environ["BREAKPOINT_API_KEY"]
            os.environ["API_KEY"] = "plain"
            self.assertEqual((backend.api_url(), backend.api_key()), ("http://example.test:1", "plain"))
        self.assertEqual(backend.api_url(), "http://127.0.0.1:8000")   # default, localhost -> 127.0.0.1


# ---------------------------------------------------------------------------
# main.py end to end, as Claude Code runs it
# ---------------------------------------------------------------------------

class EndToEnd(TempProject):
    def run_main(self, payload, extra_env=None):
        env = {**os.environ, "USERPROFILE": str(self.project), "HOME": str(self.project), **(extra_env or {})}
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        result = subprocess.run([sys.executable, str(HOOK / "main.py")], input=data, capture_output=True,
                                env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode("ascii").strip()   # must be pure ASCII (Windows cp1252 stdout)

    def bash_fail(self, cmd, err):
        return {"hook_event_name": "PostToolUseFailure", "session_id": "e2e", "cwd": str(self.project),
                "tool_name": "Bash", "tool_input": {"command": cmd}, "error": err}

    def test_known_fix_round_trip_through_main(self):
        server = FakeServer({("GET", "/api/search"): (200, [{**SOLVED, "solution": "Use the café → fix"}]),
                             ("POST", "/api/outcomes"): (200, {"post_status": "solved"})})
        self.addCleanup(server.close)
        env = {"BREAKPOINT_API_URL": server.url}
        result = json.loads(self.run_main(self.bash_fail(DC, PRISMA), env))   # the early check, first failure
        self.assertTrue(result["hookSpecificOutput"]["additionalContext"].startswith("[Breakpoint: KNOWN_FIX]"))
        self.assertIn("café", result["hookSpecificOutput"]["additionalContext"])
        passed = {"hook_event_name": "PostToolUse", "session_id": "e2e", "cwd": str(self.project),
                  "tool_name": "Bash", "tool_input": {"command": DC}, "tool_response": {"stdout": "up", "stderr": ""}}
        left = json.loads(self.run_main(passed, env))
        self.assertTrue(left["hookSpecificOutput"]["additionalContext"].startswith("[Breakpoint: LEAVE_REPLY]"))
        self.assertEqual(server.requests[-1]["body"]["worked"], True)
        self.assertTrue((self.project / ".claude" / "loop_state" / "e2e.json").exists())
        self.assertFalse((self.project / ".claude" / "loop_state" / "_errors.log").exists())

    def test_garbage_input_never_fails(self):
        self.assertEqual(self.run_main(b"\xff\xfe not json at all"), "")
        self.assertEqual(self.run_main(b""), "")
        self.assertEqual(self.run_main({"hook_event_name": "Notification", "session_id": "x"}), "")
        self.assertEqual(self.run_main({"hook_event_name": "PostToolUse", "session_id": "x",
                                        "cwd": str(self.project), "tool_name": "Bash", "tool_input": 5}), "")

    def test_specialist_sessions_do_nothing(self):
        for name in ("BREAKPOINT_SPECIALIST", "AGENTOVERFLOW_SPECIALIST"):   # the old name still works
            out = self.run_main(self.bash_fail(DC, PRISMA), {name: "1"})
            self.assertEqual(out, "")
            self.assertFalse((self.project / ".claude").exists())

    def test_handler_crash_is_logged_not_raised(self):
        env = {"PYTHONPATH": ""}
        payload = {"hook_event_name": "Stop", "session_id": "x", "cwd": str(self.project)}
        folder = self.project / ".claude" / "loop_state"
        folder.mkdir(parents=True)
        (folder / "x.json").write_text(json.dumps({"history": [5], "episode": "not a dict", "project_id": "p"}))
        self.assertEqual(self.run_main(payload, env), "")
        self.assertIn("Traceback", (folder / "_errors.log").read_text())


UNITTEST_FAIL = ("E\n======================================================================\n"
                 "ERROR: test_save (tests.test_store.StoreTests.test_save)\n"
                 "Traceback (most recent call last):\nTypeError: 'NoneType' object is not subscriptable\n"
                 "----------------------------------------------------------------------\nRan 11 tests in 0.002s\n\n"
                 "FAILED (errors=1)\n")
UNITTEST_OK = "...........\n----------------------------------------------------------------------\nRan 11 tests in 0.001s\n\nOK\n"
PATCH_AND_TEST = ("cd C:/proj && python - <<'EOF'\np='recordkit/store.py'\ns=open(p).read()\n"
                  "open(p,'w').write(s.replace('a', 'b'))\nEOF\npython -m unittest 2>&1 | tail -20; git status --short")


class CompoundCommands(TempProject):
    """Agents that chain commands: `cd x && patch <<EOF ... EOF` + `python -m unittest | tail; git status`."""

    def shell(self, cmd, out, exit_ok=True):
        if exit_ok:
            return self.send("PostToolUse", tool_name="Bash", tool_input={"command": cmd},
                             tool_response={"stdout": out, "stderr": "", "interrupted": False})
        return self.fail(cmd, "Exit code 1\n" + out)

    def runs(self):
        return [a for a in self.state()["history"] if a["kind"] == "run"]

    def test_the_check_command_is_the_key(self):
        key = struggles.command_key
        self.assertEqual(key(PATCH_AND_TEST), "python unittest")
        self.assertEqual(key("cd C:/x && sed -i 's/a/b/' src/a.py && python -m unittest 2>&1 | tail -5; git status"),
                         "python unittest")
        self.assertEqual(key("cat recordkit/*.py; python -m unittest 2>&1 | grep -E \"^(ERROR|FAIL)\""),
                         "python unittest")
        self.assertEqual(key("python -m unittest; git status"), key("python -m unittest 2>&1"))
        self.assertEqual(key("npm test && git diff"), "npm test")
        self.assertEqual(key("ls; Get-Content a.py; npx tsc --noEmit; echo done"), "npx tsc")
        self.assertEqual(key(r"cd C:\Users\me\proj; python -m pytest tests -q"), "python pytest tests")
        self.assertEqual(key("cargo test 2>&1 | tail -3"), "cargo test")
        self.assertEqual(key("go test ./... ; pwd"), "go test ./...")

    def test_probes_never_become_the_key(self):
        key = struggles.command_key
        self.assertEqual(key("python -m unittest; git status"), "python unittest")
        self.assertEqual(key("pytest -q && git log -1 && ls"), "pytest")
        self.assertTrue(struggles._is_probe(key("git status --short")))
        self.assertTrue(struggles._is_probe(key("cd src; ls; cat a.py")))
        self.assertTrue(struggles._is_probe(key("Get-ChildItem -Recurse | Select-Object -First 5")))
        self.assertEqual(key("python - <<'EOF'\nimport os\nos.system('git status')\nEOF"), "python")   # the body is skipped

    def test_split_skips_heredoc_bodies(self):
        parts = [s.text for s in struggles.split_command(PATCH_AND_TEST)]
        self.assertEqual(parts, ["cd C:/proj", "python - <<'EOF'", "python -m unittest 2>&1", "tail -20",
                                 "git status --short"])
        self.assertEqual([s.text for s in struggles.split_command("echo 'a; b' && x || y | z")],
                         ["echo 'a; b'", "x", "y", "z"])

    def test_result_comes_from_the_output_when_the_check_is_not_last(self):
        result = notebook.check_result
        self.assertIs(result("python -m unittest; git status", True, UNITTEST_FAIL), False)
        self.assertIs(result("python -m unittest; git status", True, UNITTEST_OK), True)
        self.assertIs(result("pytest -q; git status", True, "1 failed, 3 passed in 0.1s"), False)
        self.assertIs(result("pytest -q; git status", True, "4 passed in 0.1s"), True)
        self.assertIs(result("npm test; echo done", True, "npm ERR! Test failed."), False)
        self.assertIsNone(result("python -m unittest; git status", True, " M src/a.py"))   # can't tell: unknown
        self.assertIsNone(result("python -m unittest; git status", False, "fatal: not a git repository"))
        self.assertIs(result("python -m unittest && git status", True, " M a.py"), True)   # && ran on: it passed
        self.assertIs(result("python -m unittest", False, UNITTEST_OK), False)             # last part: the exit code
        self.assertIsNone(result("python -m unittest 2>&1 | grep FAIL", False, ""))       # grep found nothing
        # A patch script's traceback doesn't hide the runner's own "OK".
        self.assertIs(result("python - <<'EOF'\nx\nEOF\npython -m unittest 2>&1 | tail -3; git status", True,
                             "Traceback (most recent call last):\nNameError: x\nRan 11 tests\n\nOK"), True)

    def test_unknown_result_records_no_run(self):
        self.shell("python -m unittest; git status", " M src/a.py")
        self.assertEqual(self.runs(), [])
        self.shell("python -m unittest; git status", UNITTEST_FAIL)
        self.assertEqual([(r["key"], r["ok"]) for r in self.runs()], [("python unittest", False)])

    def test_shell_writes_count_as_edits(self):
        edits = notebook.shell_edits
        self.assertEqual(edits(PATCH_AND_TEST), ["recordkit/store.py"])
        self.assertEqual(edits("sed -i 's/a/b/' src/a.py src/b.py && pytest"), ["src/a.py", "src/b.py"])
        self.assertEqual(edits("sed -i.bak -e 's/a/b/' src/a.py"), ["src/a.py"])
        self.assertEqual(edits("(Get-Content a.ps1) -replace 'x','y' | Set-Content -Path a.ps1"), ["a.ps1"])
        self.assertEqual(edits("'x' | Out-File notes.txt"), ["notes.txt"])
        self.assertEqual(edits("cat > src/new.py <<'EOF'\nprint(1)\nEOF"), ["src/new.py"])
        self.assertEqual(edits("echo x >> log.txt; echo y > /dev/null; pytest 2>&1 > $null"), ["log.txt"])
        self.assertEqual(edits("echo a | tee -a out.txt"), ["out.txt"])
        self.assertEqual(edits("python -c \"from pathlib import Path; Path('cfg.toml').write_text('x')\""),
                         ["cfg.toml"])
        self.assertEqual(edits("python - <<'EOF'\nprint(open('a.py').read())\nEOF"), [])   # reads only
        self.assertEqual(edits("python -m unittest 2>&1 | tail -5"), [])

    def test_edits_are_recorded_before_the_run(self):
        self.shell(PATCH_AND_TEST, UNITTEST_FAIL)
        history = self.state()["history"]
        self.assertEqual([(a["kind"], a.get("path") or a.get("key")) for a in history],
                         [("edit", "recordkit/store.py"), ("run", "python unittest")])
        self.assertEqual(history[0]["via"], "shell")

    def test_one_call_per_try_still_climbs_the_ladder(self):
        """An agent that patches and tests in one call (1 action per try): STEP_BACK, then the team step (the stuck
        menu) 2 failures later; no message on the failure in between."""
        outputs = [self.shell(PATCH_AND_TEST, UNITTEST_FAIL) for _ in range(5)]
        self.assertEqual([tag(o) for o in outputs], [None, None, "STEP_BACK", None, "STUCK_MENU"])
        self.assertEqual(self.state()["episode"]["key"], "python unittest")

    def test_a_new_error_after_a_message_climbs_right_away(self):
        """Whack-a-mole: each fix reveals a new error, so the next rung doesn't wait a second failure."""
        errors = ["KeyError: 'sku'", "AttributeError: no Num", "ValueError: bad binding", "LookupError: no row",
                  "IndexError: lines"]
        outputs = [self.shell(PATCH_AND_TEST, UNITTEST_FAIL.replace(
            "TypeError: 'NoneType' object is not subscriptable", e)) for e in errors]
        # 4 failures, 4 different errors: whack-a-mole -> STEP_BACK; the 5th error is new -> the team step at once.
        self.assertEqual([tag(o) for o in outputs], [None, None, None, "STEP_BACK", "STUCK_MENU"])

    def test_edit_run_same_output_sees_shell_patches(self):
        for _ in range(4):
            self.shell(PATCH_AND_TEST, UNITTEST_FAIL)
        names = [s.name for s in struggles.check_after_run(self.state()["history"])]
        self.assertIn("edit_run_same_output", names)

    def test_rewind_offer_lets_the_next_failure_climb(self):
        state = {"history": [{"kind": "run", "key": "pytest", "ok": False}] * 2, "episode": None}
        episode = notebook.new_episode("pytest", "f", 0)
        episode.update(last_nudge_idx=2, rewind_offer_idx=2)
        state["history"].append({"kind": "run", "key": "pytest", "ok": False})
        self.assertTrue(brain._cooled_down(state, episode))       # 1 failure after a REWIND_OFFER is enough
        episode["rewind_offer_idx"] = None
        self.assertFalse(brain._cooled_down(state, episode))      # after other messages: 2 failures or 3 actions
        state["history"].append({"kind": "run", "key": "git status", "ok": False})
        self.assertFalse(brain._cooled_down(state, episode))      # a probe is not a failure
        state["history"].append({"kind": "run", "key": "pytest", "ok": False})
        self.assertTrue(brain._cooled_down(state, episode))

    def test_no_not_done_after_the_check_really_passed(self):
        self.shell(PATCH_AND_TEST, UNITTEST_FAIL)
        self.shell("python -m unittest tests.test_store", UNITTEST_FAIL, exit_ok=False)
        self.shell("python - <<'EOF'\nraise SystemExit(1)\nEOF", "Traceback (most recent call last):\nSystemExit",
                   exit_ok=False)
        self.shell("cd C:/proj && python -m unittest 2>&1 | tail -5", UNITTEST_OK)
        self.assertIsNone(self.send("Stop", stop_hook_active=False))

    def test_not_done_still_fires_when_the_check_fails_last(self):
        self.shell("python -m unittest", UNITTEST_OK)
        self.shell(PATCH_AND_TEST, UNITTEST_FAIL)
        self.assertEqual(tag(self.send("Stop", stop_hook_active=False)), "NOT_DONE")


# ---------------------------------------------------------------------------
# The stuck menu (team_mode ask) and the team started by the hook (auto)
# ---------------------------------------------------------------------------

STARTED = messages.COMMUNITY_LABEL + "TEAM_STARTED: run-5\nThe team is researching. Check back in about 60s."
FAKE_REWIND = [{"name": "episode_start", "title": "when this problem started", "cmd": DC, "time": "7:42 PM, 3 min ago",
                "edits": 3, "changed": ["Dockerfile"], "added": [], "removed": [], "files": 1,
                "command": "python rewind.py --session test-session episode_start"}]


class StuckMenu(TempProject):
    def stuck(self, n=3):
        for _ in range(n):
            out = self.fail(DC, PRISMA)
        return out

    def ask(self, label=None, response=None, event="PostToolUse"):
        """The AskUserQuestion tool's result, in the shape Claude Code gives it (questions + answers)."""
        question = (self.state().get("pending_menu") or {}).get("question", "q?")
        tool_input = {"questions": [{"question": question, "header": "Stuck", "multiSelect": False,
                                     "options": [{"label": messages.MENU_OPTIONS[n][0], "description": "d"}
                                                 for n in ("team", "keep")]}]}
        if response is None:
            response = {"questions": tool_input["questions"], "answers": {question: label}}
        fields = {"tool_response": response} if event == "PostToolUse" else {"error": "not available"}
        return self.send(event, tool_name="AskUserQuestion", tool_input=tool_input, **fields)

    def pre(self, tool):
        return self.send("PreToolUse", tool_name=tool, tool_input={})

    def helps(self):
        return [c for c in self.backend.calls if c[0] == "help"]

    def test_menu_is_offered_in_ask_mode(self):
        out = self.stuck()
        self.assertEqual(tag(out), "STUCK_MENU")
        text = text_of(out)
        self.assertIn("call the AskUserQuestion tool NOW", text)
        self.assertIn('label: "Call the specialist team"', text)
        self.assertIn('label: "Let the agent keep trying"', text)
        self.assertNotIn("Rewind the files", text)   # no checkpoint: no rewind option
        self.assertIn(f"`{DC}` failed 3 times with 1 different error", text)
        menu = self.state()["pending_menu"]
        self.assertEqual(menu["options"], ["team", "keep"])
        self.assertEqual(self.helps(), [])            # nothing is started before the user answers
        self.assertTrue(self.state()["episode"]["rung"] >= 3)
        for _ in range(4):                            # never offered twice in one episode
            self.edit()
            self.assertIsNone(self.fail(DC, PRISMA))

    def test_menu_offers_the_rewind_when_one_is_possible(self):
        with mock.patch.object(brain, "_rewind_options", return_value=FAKE_REWIND):
            out = self.stuck()
        self.assertEqual(tag(out), "STUCK_MENU")
        self.assertIn('2. label: "Rewind the files to before the problem started"', text_of(out))
        self.assertIn("undoes 3 edits", text_of(out))
        self.assertEqual(self.state()["pending_menu"]["options"], ["team", "rewind", "keep"])
        self.assertTrue(self.state()["episode"]["rewind_offered"])

    def test_pre_tool_use_stops_any_other_tool(self):
        self.assertIsNone(self.pre("Bash"))           # no menu: nothing happens
        self.stuck()
        for allowed in ("AskUserQuestion", "ToolSearch", "Skill"):
            self.assertIsNone(self.pre(allowed))
        out = self.pre("Bash")
        self.assertEqual(out["continue"], False)
        self.assertIn("1. Call the specialist team", out["stopReason"])
        self.assertIn("2. Let the agent keep trying", out["stopReason"])
        self.assertIn("Type 1 or 2.", out["stopReason"])
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")   # the tool itself never runs
        self.assertTrue(self.state()["pending_menu"]["stopped"])
        self.assertEqual(self.pre("mcp__plugin_breakpoint_breakpoint__ask_for_help")["continue"], False)

    def test_stop_while_the_menu_waits_asks_now_once(self):
        out = self.stuck()
        self.assertIn("Type 1 or 2.", text_of(out))   # the text fallback is in the menu message too
        out = self.stop()
        self.assertEqual(tag(out), "STUCK_MENU")      # not NOT_DONE ("keep fixing") while the user must choose
        self.assertIn("word for word", text_of(out))
        self.assertIn("1. Call the specialist team", text_of(out))
        self.assertIsNone(self.stop(active=True))
        self.assertIsNone(self.stop())                # reminded once; then quiet while the menu waits
        self.user("1")                                # answered (the fake backend can't start the team)
        self.fail(DC, PRISMA)
        self.assertEqual(tag(self.stop()), "NOT_DONE")   # the usual Stop checks again

    def test_no_stop_reminder_when_the_reply_shows_the_menu(self):
        self.stuck()
        self.say("Here is the menu:\n" + messages.typed_menu(self.state()["pending_menu"]["question"], ["team", "keep"]))
        self.assertIsNone(self.stop())
        self.assertIsNotNone(self.state()["pending_menu"])

    def test_pre_tool_use_through_main_is_a_quick_no_op(self):
        import main
        payload = {"hook_event_name": "PreToolUse", "session_id": "nofile", "cwd": str(self.project),
                   "tool_name": "Bash", "tool_input": {"command": "ls"}}
        self.assertIsNone(main.run(json.dumps(payload).encode()))
        self.assertFalse((self.project / ".claude").exists())   # no notes file made, no git run
        self.stuck()
        out = main.run(json.dumps({**payload, "session_id": self.sid}).encode())
        self.assertEqual(out["continue"], False)

    def test_answer_team_starts_the_team_in_the_hook(self):
        self.backend.help_result = STARTED
        self.edit("Dockerfile")
        self.stuck()
        out = self.ask("Call the specialist team")
        self.assertEqual(tag(out), "TEAM_CALLED")
        self.assertIn("already working on this (run run-5)", text_of(out))
        self.assertIn("call `check_help` with run_id run-5", text_of(out))
        self.assertEqual(self.helps(), [("help", DC, struggles.fingerprint(PRISMA))])
        case = self.backend.cases[0]
        self.assertIn("PrismaClientInitializationError", case["symptom"])
        self.assertIn("(3 failed runs; different errors: 1)", case["goal"])
        self.assertTrue(case["tried"] and case["tried"][0]["attempt"])
        episode = self.state()["episode"]
        self.assertEqual((episode["team_ran"], episode["team_run_id"], episode["team_status"]),
                         (True, "run-5", "working"))
        self.assertIsNone(self.state()["pending_menu"])
        self.assertIsNone(self.pre("Bash"))           # answered: tools run again
        # the existing flow carries on: check_help -> TEAM_REPORT -> TEST_FIXES
        self.assertEqual(tag(self.mcp("check_help", "TEAM_REPORT: run run-5\n1. cause")), "TEST_FIXES")

    def test_case_file_names_recently_edited_files(self):
        (self.project / "Dockerfile").write_text("FROM node:20-alpine\n")
        self.backend.help_result = STARTED
        self.edit("Dockerfile")
        self.stuck()
        self.ask("Call the specialist team")
        case = self.backend.cases[0]
        self.assertEqual([f["path"] for f in case.get("files", [])], ["Dockerfile"])
        self.assertIn("OS:", case["environment"])

    def test_answer_rewind_hands_over_the_rewind_offer(self):
        with mock.patch.object(brain, "_rewind_options", return_value=FAKE_REWIND):
            self.stuck()
        out = self.ask(response="User has answered your questions: \"q?\"=\"Rewind the files to before the problem "
                                "started\". You can now continue with the user's answers in mind.")
        self.assertEqual(tag(out), "REWIND_OFFER")
        self.assertIn("the user already said yes in the Breakpoint menu", text_of(out))
        self.assertIn("Run the `episode_start` option's command now", text_of(out))
        self.assertIn("command: python rewind.py --session test-session episode_start", text_of(out))
        self.assertEqual(self.helps(), [])
        self.assertIsNone(self.state()["pending_menu"])

    def test_answer_keep_trying_is_quiet(self):
        self.stuck()
        self.assertIsNone(self.ask("Let the agent keep trying"))
        self.assertIsNone(self.state()["pending_menu"])
        for _ in range(4):   # no repeat menu, no team, nothing until the next rung is due
            self.edit()
            self.assertIsNone(self.fail(DC, PRISMA))
        self.assertEqual(self.helps(), [])

    def test_a_failed_question_keeps_the_menu_waiting(self):
        self.stuck()
        self.assertIsNone(self.ask(event="PostToolUseFailure"))
        self.assertIsNotNone(self.state()["pending_menu"])
        self.assertEqual(self.pre("Bash")["continue"], False)

    def test_an_answer_of_their_own_drops_the_menu(self):
        self.stuck()
        out = self.ask("Look at the Dockerfile first")
        self.assertEqual(tag(out), "TEAM_STATUS")
        self.assertIsNone(self.state()["pending_menu"])
        self.assertEqual(self.helps(), [])

    def test_typed_answer_after_the_stop(self):
        self.backend.help_result = STARTED
        self.stuck()
        self.assertEqual(self.pre("Bash")["continue"], False)
        out = self.user("1")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(tag(out), "TEAM_CALLED")
        self.assertIn('The user typed "1"', text_of(out))
        self.assertEqual(len(self.helps()), 1)
        self.assertTrue(self.state()["episode"]["team_ran"])

    def test_typed_words_and_keep_trying(self):
        self.stuck()
        out = self.user("keep trying")
        self.assertEqual(tag(out), "TEAM_STATUS")
        self.assertIn("let the agent keep trying", text_of(out))
        self.assertEqual(self.helps(), [])
        self.assertEqual(brain._menu_choice("2", ["team", "rewind", "keep"], numbers=True), "rewind")
        self.assertEqual(brain._menu_choice("option 3.", ["team", "rewind", "keep"], numbers=True), "keep")
        self.assertIsNone(brain._menu_choice("3", ["team", "keep"], numbers=True))
        self.assertIsNone(brain._menu_choice("Call the specialist team / Let the agent keep trying", ["team", "keep"]))

    def test_a_different_prompt_drops_the_menu(self):
        self.stuck()
        self.assertIsNone(self.user("Please also add a README while you are at it, with setup steps for Windows"))
        self.assertIsNone(self.state()["pending_menu"])
        self.assertIsNone(self.pre("Bash"))
        self.assertEqual(self.helps(), [])

    def test_failed_team_start_falls_back_to_ask_for_help(self):
        self.backend.help_result = None   # server down or an error
        self.stuck()
        out = self.ask("Call the specialist team")
        self.assertEqual(tag(out), "CALL_TEAM")
        self.assertIn("the request failed: call `ask_for_help` yourself now", text_of(out))
        self.assertFalse(self.state()["episode"]["team_ran"])
        self.assertIsNone(self.state()["pending_menu"])

    def test_server_answers_known_fix_instead_of_a_run(self):
        self.backend.help_result = "KNOWN_FIX: forum post #4 matches this problem (same error fingerprint)."
        self.stuck()
        out = self.ask("Call the specialist team")
        self.assertEqual(tag(out), "TEAM_CALLED")
        self.assertIn("KNOWN_FIX: forum post #4", text_of(out))
        self.assertFalse(self.state()["episode"]["team_ran"])
        self.assertEqual(self.state()["episode"]["pending_fix"]["post_id"], 4)

    def test_auto_starts_the_team_directly(self):
        os.environ["BREAKPOINT_TEAM_MODE"] = "auto"
        self.backend.help_result = STARTED
        out = self.stuck()
        self.assertEqual(tag(out), "TEAM_CALLED")
        self.assertIn("team_mode is auto", text_of(out))
        self.assertEqual(len(self.helps()), 1)
        self.assertIsNone(self.state()["pending_menu"])
        self.assertEqual(self.state()["episode"]["team_run_id"], "run-5")

    def test_auto_failed_start_falls_back_to_call_team(self):
        os.environ["BREAKPOINT_TEAM_MODE"] = "auto"
        self.assertEqual(tag(self.stuck()), "CALL_TEAM")
        self.assertEqual(len(self.helps()), 1)

    def test_off_never_calls_the_team(self):
        os.environ["BREAKPOINT_TEAM_MODE"] = "off"
        self.backend.help_result = STARTED
        outs = [self.stuck(3)] + [self.fail(DC, PRISMA) for _ in range(6)]
        self.assertEqual([tag(o) for o in outs if o], [])
        self.assertEqual(self.helps(), [])
        self.assertIsNone(self.state()["pending_menu"])

    def test_off_keeps_the_rewind_offer(self):
        os.environ["BREAKPOINT_TEAM_MODE"] = "off"
        with mock.patch.object(brain, "_rewind_options", return_value=FAKE_REWIND):
            self.assertEqual(tag(self.stuck()), "REWIND_OFFER")

    def test_team_mode_setting(self):
        config = self.project / "config.json"
        with mock.patch.object(backend, "CONFIG_PATH", config):
            del os.environ["BREAKPOINT_TEAM_MODE"]
            self.assertEqual(backend.team_mode(), "ask")   # no file: the default
            config.write_text(json.dumps({"team_mode": "auto"}))
            self.assertEqual(backend.team_mode(), "auto")
            os.environ["BREAKPOINT_TEAM_MODE"] = " OFF "    # env wins
            self.assertEqual(backend.team_mode(), "off")
            os.environ["BREAKPOINT_TEAM_MODE"] = "local"    # an old value: the default
            self.assertEqual(backend.team_mode(), "ask")


class AskHelpContract(TempProject):
    def test_ask_help_posts_the_case_file(self):
        self.server = FakeServer({})
        self.addCleanup(self.server.close)
        os.environ["BREAKPOINT_API_URL"] = self.server.url
        os.environ["BREAKPOINT_API_KEY"] = "k1"
        patcher = mock.patch.object(backend, "CONFIG_PATH", self.project / "no_config.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server.routes[("POST", "/api/help")] = (200, {"message": "TEAM_STARTED: run-1\n...", "run_id": "run-1"})
        case = {"goal": "g", "symptom": "token=abc123 failed", "tried": [{"attempt": "a", "result": "r"}]}
        self.assertEqual(backend.ask_help(case), "TEAM_STARTED: run-1\n...")
        req = self.server.requests[-1]
        self.assertEqual((req["method"], req["path"], req["key"]), ("POST", "/api/help", "k1"))
        self.assertEqual(req["body"]["symptom"], "token=<secret> failed")   # scrubbed
        self.assertEqual(req["body"]["tried"], [{"attempt": "a", "result": "r"}])
        self.server.routes[("POST", "/api/help")] = (500, {"detail": "boom"})
        self.assertIsNone(backend.ask_help(case))


if __name__ == "__main__":
    unittest.main()
