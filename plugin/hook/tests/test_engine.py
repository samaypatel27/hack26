"""Tests for the hook engine: notebook.py, backend.py, messages.py, brain.py, main.py.

Run from the repo root: python -m unittest discover -s plugin/hook/tests
The brain tests use a fake backend object; the backend and end-to-end tests use a tiny local HTTP server.
"""

import json
import os
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

ENV_KEYS = ["CLAUDE_PROJECT_DIR", "AGENTOVERFLOW_API_URL", "AGENTOVERFLOW_API_KEY", "API_URL", "API_KEY",
            "AGENTOVERFLOW_SPECIALIST"]

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
    """The first [AgentOverflow: TAG] in a hook output, or None."""
    if not output:
        return None
    text = output.get("reason") or output["hookSpecificOutput"]["additionalContext"]
    return text.split("]")[0].replace("[AgentOverflow: ", "")


def text_of(output):
    return output.get("reason") or output["hookSpecificOutput"]["additionalContext"]


class FakeBackend:
    def __init__(self, search=None, judge=None):
        self.search_result, self.judge_result = search, judge
        self.calls = []

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

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="ao_test_"))
        self.addCleanup(shutil.rmtree, self.project, True)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)
        self.transcript = self.project / "transcript.jsonl"
        self.sid = "test-session"
        self.backend = FakeBackend(search=[])

    # -- sending events through the brain, the way main.py does -------------------------------
    def send(self, event, **fields):
        payload = {"hook_event_name": event, "session_id": self.sid, "cwd": str(self.project),
                   "transcript_path": str(self.transcript), **fields}
        handler = {"SessionStart": brain.on_session_start, "UserPromptSubmit": brain.on_user_prompt,
                   "PostToolUse": brain.on_tool_use, "PostToolUseFailure": brain.on_tool_use,
                   "Stop": brain.on_stop}[event]
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
        return self.send("PostToolUse", tool_name=f"mcp__plugin_agentoverflow_agentoverflow__{name}",
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
            self.assertRegex(text.splitlines()[0], r"^\[AgentOverflow: [A-Z_]+\] \S")
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

class Ladder(TempProject):
    def test_known_fix_then_outcome_worked(self):
        self.backend.search_result = [SOLVED]
        self.assertIsNone(self.fail(DC, PRISMA))
        self.edit("Dockerfile")
        self.assertIsNone(self.fail(DC, PRISMA))
        out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "KNOWN_FIX")
        self.assertIn(("search", DC, struggles.fingerprint(PRISMA)), self.backend.calls)
        self.assertEqual(self.state()["episode"]["pending_fix"],
                         {"post_id": 1, "key": "docker compose up", "fp": struggles.fingerprint(PRISMA)})
        self.edit("prisma/schema.prisma")
        self.assertIsNone(self.ok(DC, "listening on :3000"))
        self.assertIn(("outcome", 1, True), self.backend.calls)
        self.assertIsNone(self.state()["episode"])

    def test_fix_failed_reports_and_moves_on(self):
        self.backend.search_result = [SOLVED]
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "KNOWN_FIX")
        self.edit("Dockerfile")
        out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "FIX_FAILED")
        self.assertIn(("outcome", 1, False), self.backend.calls)
        self.assertIn("[AgentOverflow: CALL_TEAM]", text_of(out))   # node_modules in the trace: external
        self.assertIn("forum fix from post #1 -> did not work", self.state()["episode"]["tried"])

    def test_no_match_external_calls_the_team(self):
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "CALL_TEAM")
        self.assertIn("ask_for_help", text_of(out))
        self.assertIn("already tried:", text_of(out))

    def test_backend_down_still_climbs(self):
        self.backend.search_result = None
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "CALL_TEAM")
        self.assertIn("ask_for_help", text_of(out))

    def test_open_question_then_team_next_time(self):
        self.backend.search_result = [OPEN]
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "OPEN_QUESTION_EXISTS")
        self.assertIn("#3", text_of(out))
        self.edit()
        self.assertIsNone(self.fail(DC, PRISMA))   # cooldown
        out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "CALL_TEAM")

    def test_weak_text_match_is_not_a_known_fix(self):
        self.backend.search_result = [{**SOLVED, "match": "text", "score": 0.3}]
        for _ in range(3):
            out = self.fail(DC, PRISMA)
        self.assertEqual(tag(out), "CALL_TEAM")

    def test_own_code_steps_back_then_calls_team(self):
        for _ in range(3):
            out = self.fail(PT, PYTEST)
        self.assertEqual(tag(out), "STEP_BACK")
        self.edit("cart.py")
        self.assertIsNone(self.fail(PT, PYTEST))   # cooldown: only 2 actions since the nudge
        self.edit("cart.py")
        self.assertEqual(tag(self.fail(PT, PYTEST)), "CALL_TEAM")

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
        self.assertEqual(tag(out), "CALL_TEAM")
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

    def test_known_fix_shows_the_useful_count(self):
        """The real tables keep "useful" marks, not times worked/applied."""
        text = messages.known_fix(SOLVED, DC, "abc", [])
        self.assertTrue(text.startswith("[AgentOverflow: KNOWN_FIX]"))
        self.assertIn("(marked useful by 9 agents)", text.splitlines()[0])
        self.assertIn("marked useful by 1 agent", messages._proof({"useful_count": 1}))
        self.assertIn("worked 9 of 10 times", messages._proof({"times_worked": 9, "times_applied": 10}))

    def test_probe_commands_never_struggle(self):
        for _ in range(5):
            self.assertIsNone(self.fail("grep -r foo .", "Exit code 1"))
        self.assertEqual(self.backend.calls, [])


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
        os.environ["AGENTOVERFLOW_API_URL"] = self.server.url
        os.environ["AGENTOVERFLOW_API_KEY"] = "k1"
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
        os.environ["AGENTOVERFLOW_API_URL"] = f"http://127.0.0.1:{port}"
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
            del os.environ["AGENTOVERFLOW_API_URL"], os.environ["AGENTOVERFLOW_API_KEY"]
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
        env = {"AGENTOVERFLOW_API_URL": server.url}
        outputs = [self.run_main(self.bash_fail(DC, PRISMA), env) for _ in range(3)]
        self.assertEqual(outputs[:2], ["", ""])
        result = json.loads(outputs[2])
        self.assertTrue(result["hookSpecificOutput"]["additionalContext"].startswith("[AgentOverflow: KNOWN_FIX]"))
        self.assertIn("café", result["hookSpecificOutput"]["additionalContext"])
        passed = {"hook_event_name": "PostToolUse", "session_id": "e2e", "cwd": str(self.project),
                  "tool_name": "Bash", "tool_input": {"command": DC}, "tool_response": {"stdout": "up", "stderr": ""}}
        self.assertEqual(self.run_main(passed, env), "")
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
        out = self.run_main(self.bash_fail(DC, PRISMA), {"AGENTOVERFLOW_SPECIALIST": "1"})
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


if __name__ == "__main__":
    unittest.main()
