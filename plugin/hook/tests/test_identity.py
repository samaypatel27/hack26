"""Tests for accounts on the plugin side (docs/accounts.md): identity.py, the identity headers and
registration in backend.py, the SessionStart sync in brain.py, setup_identity.py, and the small
accounts edits in mcp/server.py.

Run from the repo root: python -m unittest discover -s plugin/hook/tests
Every test uses a temporary config file, never the real ~/.breakpoint/config.json.
"""

import contextlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

HOOK = Path(__file__).resolve().parents[1]
PLUGIN = HOOK.parent
sys.path.insert(0, str(HOOK))

import backend  # noqa: E402
import brain  # noqa: E402
import identity  # noqa: E402
import notebook  # noqa: E402
import setup_identity  # noqa: E402

# The backend's agent ID rule (backend/activity.py AgentRef): "<model-slug>_<lowercase UUID>".
AGENT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ENV_KEYS = ["CLAUDE_PROJECT_DIR", "BREAKPOINT_PROJECT_DIR", "BREAKPOINT_API_URL", "BREAKPOINT_API_KEY",
            "AGENTOVERFLOW_PROJECT_DIR", "AGENTOVERFLOW_API_URL", "AGENTOVERFLOW_API_KEY",   # the old names
            "API_URL", "API_KEY", "AGENT_MODEL", "AGENT_ID", "ANTHROPIC_MODEL", "CLAUDE_PLUGIN_OPTION_EMAIL"]


class Isolated(unittest.TestCase):
    """A temp project folder and a temp config file, with our env settings cleared."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ao_ident_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.project = self.tmp / "project"
        self.project.mkdir()
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)
        self.config = self.tmp / "home" / ".breakpoint" / "config.json"
        for target in (backend, identity):
            patcher = mock.patch.object(target, "CONFIG_PATH", self.config)
            patcher.start()
            self.addCleanup(patcher.stop)
        identity._project_id_for.cache_clear()

    def read(self):
        return json.loads(self.config.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# identity.py
# ---------------------------------------------------------------------------

class Identity(Isolated):
    def test_created_once_and_kept(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"api_url": "http://x.test", "team_mode": "local"}), encoding="utf-8")
        first = identity.ensure()
        self.assertTrue(identity._is_uuid(first["install_id"]))
        self.assertNotIn("install_secret", first)   # no secret any more (no column for it on the server)
        self.assertEqual(identity.ensure()["install_id"], first["install_id"])     # same ID next time
        saved = self.read()
        self.assertEqual((saved["api_url"], saved["team_mode"]), ("http://x.test", "local"))   # other keys kept

    def test_two_installs_differ(self):
        one = identity.ensure()["install_id"]
        other = identity.ensure(self.tmp / "other.json")["install_id"]
        self.assertNotEqual(one, other)

    def test_agent_id_format_and_model_sources(self):
        install = identity.ensure()["install_id"]
        self.assertEqual(identity.agent_id(), f"unknown_{install}")                 # nothing known yet
        os.environ["ANTHROPIC_MODEL"] = "claude-haiku-5-5"
        self.assertEqual(identity.current_model(), "claude-haiku-5-5")
        identity.update_config({"model": "claude-opus-5-5"})
        self.assertEqual(identity.current_model(), "claude-opus-5-5")               # the hook's beats env
        os.environ["AGENT_MODEL"] = "claude-sonnet-5-5[1m]"
        self.assertEqual(identity.current_model(), "claude-sonnet-5-5")             # AGENT_MODEL wins
        agent = identity.agent_id()
        self.assertRegex(agent, AGENT_ID_RE)
        self.assertEqual(agent, f"claude-sonnet-5-5_{install}")
        self.assertEqual(identity.short_name(agent), f"agent-{install.replace('-', '')[:4]}")
        self.assertEqual(identity.public_name(), f"agent-{install[:4]} (claude-sonnet-5-5)")
        self.assertNotIn(install, identity.public_name())

    def test_email_set_validate_remove(self):
        identity.set_email(" Me@OSU.EDU ")
        self.assertEqual((self.read()["email"], self.read()["email_source"]), ("Me@osu.edu", "setup"))
        with self.assertRaises(ValueError):
            identity.set_email("not an email")
        self.assertEqual(self.read()["email"], "Me@osu.edu")                        # bad input changes nothing
        identity.set_email(None)
        self.assertNotIn("email", self.read())
        self.assertEqual(identity.mask_email("harsh@osu.edu"), "h***@osu.edu")

    def test_session_sync_saves_model_and_plugin_option_email(self):
        identity.sync_from_session({"model": "claude-opus-5-5"})
        self.assertEqual(self.read()["model"], "claude-opus-5-5")
        identity.sync_from_session({})                                               # no model: keep the old one
        self.assertEqual(self.read()["model"], "claude-opus-5-5")
        os.environ["CLAUDE_PLUGIN_OPTION_EMAIL"] = "opt@osu.edu"
        identity.sync_from_session({})
        self.assertEqual((self.read()["email"], self.read()["email_source"]), ("opt@osu.edu", "plugin_option"))
        os.environ["CLAUDE_PLUGIN_OPTION_EMAIL"] = ""                                # cleared in /plugin configure
        identity.sync_from_session({})
        self.assertNotIn("email", self.read())
        identity.set_email("setup@osu.edu")                                          # a /setup email stays
        identity.sync_from_session({})
        self.assertEqual(self.read()["email"], "setup@osu.edu")

    def test_headers(self):
        identity.update_config({"model": "claude-sonnet-5-5"})
        h = identity.headers()
        self.assertRegex(h["X-Agent-Id"], AGENT_ID_RE)
        self.assertEqual(h["X-Agent-Model"], "claude-sonnet-5-5")
        self.assertEqual(h["X-Project-Id"], notebook.project_id(self.project))      # the hook's own project rule
        self.assertNotIn("X-Agent-Secret", h)                                        # no install secret any more
        self.assertFalse(hasattr(identity, "secret_header"))

    def test_project_dir_for_the_mcp_server(self):
        del os.environ["CLAUDE_PROJECT_DIR"]
        os.environ["BREAKPOINT_PROJECT_DIR"] = str(self.project)
        self.assertEqual(identity.project_folder(), self.project)
        os.environ["BREAKPOINT_PROJECT_DIR"] = "${CLAUDE_PROJECT_DIR}"               # not expanded: ignore it
        self.assertEqual(identity.project_folder(), Path.cwd())
        del os.environ["BREAKPOINT_PROJECT_DIR"]
        os.environ["AGENTOVERFLOW_PROJECT_DIR"] = str(self.project)                  # the old name still works
        self.assertEqual(identity.project_folder(), self.project)

    def test_old_config_folder_is_copied_once(self):
        """Before the rename the file was ~/.agentoverflow/config.json: an old install keeps its ID."""
        home = self.tmp / "realhome"
        new, old = home / ".breakpoint" / "config.json", home / ".agentoverflow" / "config.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({"install_id": "11111111-2222-4333-8444-555555555555", "api_key": "k"}),
                       encoding="utf-8")
        with mock.patch.object(identity, "CONFIG_PATH", new), mock.patch.object(identity, "HOME_CONFIG_PATH", new), \
                mock.patch.object(identity, "OLD_CONFIG_PATH", old):
            self.assertEqual(identity.read_config()["api_key"], "k")
            self.assertTrue(new.is_file() and old.is_file())                        # copied; the old file stays
            identity.update_config({"api_key": "k2"})
            self.assertEqual(identity.ensure()["install_id"], "11111111-2222-4333-8444-555555555555")
            self.assertEqual(json.loads(old.read_text(encoding="utf-8"))["api_key"], "k")   # never copied again
            self.assertEqual(identity.read_config()["api_key"], "k2")
        self.assertFalse(identity.copy_old_config(self.tmp / "a" / "c.json", self.tmp / "missing.json"))

    def test_env_names_new_then_old(self):
        os.environ["AGENTOVERFLOW_API_URL"] = "http://old.test"
        self.assertEqual(backend.api_url(), "http://old.test")
        os.environ["BREAKPOINT_API_URL"] = "http://new.test"
        self.assertEqual(backend.api_url(), "http://new.test")
        self.assertEqual(identity.env("API_URL"), "http://new.test")

    def test_registration_memory(self):
        body = identity.registration_body()
        self.assertEqual(set(body), {"agent_id", "model_name", "email"})
        self.assertTrue(identity.needs_registration("http://a", body))
        identity.mark_registered("http://a", body)
        self.assertFalse(identity.needs_registration("http://a", body))
        self.assertTrue(identity.needs_registration("http://b", body))               # another server
        identity.set_email("a@b.dev")
        self.assertTrue(identity.needs_registration("http://a", identity.registration_body()))


# ---------------------------------------------------------------------------
# backend.py: headers on every call, and registration at SessionStart
# ---------------------------------------------------------------------------

class Recorder:
    """A tiny server that records each request's path, headers and body."""

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
                                       "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": json.loads(body) if body else None})
                status, reply = outer.routes.get((method, url.path), (404, {"detail": "not found"}))
                data = json.dumps(reply).encode()
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


class HookBackend(Isolated):
    def setUp(self):
        super().setUp()
        self.server = Recorder({
            ("GET", "/api/search"): (200, []),
            ("POST", "/api/agents/register"): (200, {"agent_id": "x", "email_on_file": True}),
        })
        self.addCleanup(self.server.close)
        os.environ["BREAKPOINT_API_URL"] = self.server.url
        os.environ["BREAKPOINT_API_KEY"] = "k1"

    def test_every_call_carries_the_identity(self):
        backend.search("boom", "npm test", "abcd1234")
        h = self.server.requests[-1]["headers"]
        self.assertEqual(h["x-api-key"], "k1")
        self.assertRegex(h["x-agent-id"], AGENT_ID_RE)
        self.assertEqual(h["x-project-id"], notebook.project_id(self.project))
        self.assertNotIn("x-agent-secret", h)
        self.assertTrue(identity._is_uuid(self.read()["install_id"]))                # created on first use

    def test_session_start_registers_once_then_on_change(self):
        os.environ["CLAUDE_PLUGIN_OPTION_EMAIL"] = "me@osu.edu"
        backend.sync_identity({"model": "claude-sonnet-5-5"})
        reg = [r for r in self.server.requests if r["path"] == "/api/agents/register"]
        self.assertEqual(len(reg), 1)
        self.assertNotIn("x-agent-secret", reg[0]["headers"])
        self.assertEqual(reg[0]["body"]["model_name"], "claude-sonnet-5-5")
        self.assertEqual(reg[0]["body"]["email"], "me@osu.edu")
        self.assertRegex(reg[0]["body"]["agent_id"], AGENT_ID_RE)
        backend.sync_identity({"model": "claude-sonnet-5-5"})                        # nothing changed
        self.assertEqual(len([r for r in self.server.requests if r["path"] == "/api/agents/register"]), 1)
        backend.sync_identity({"model": "claude-opus-5-5"})                          # new model: new agent ID
        reg = [r for r in self.server.requests if r["path"] == "/api/agents/register"]
        self.assertEqual(len(reg), 2)
        self.assertTrue(reg[1]["body"]["agent_id"].startswith("claude-opus-5-5_"))

    def test_failed_registration_is_retried_and_never_raises(self):
        self.server.routes[("POST", "/api/agents/register")] = (503, {"detail": "not set up"})
        self.assertIsNone(backend.sync_identity({"model": "claude-sonnet-5-5"}))
        self.server.routes[("POST", "/api/agents/register")] = (200, {"agent_id": "x"})
        self.assertEqual(backend.sync_identity({"model": "claude-sonnet-5-5"}), {"agent_id": "x"})

    def test_brain_session_start_syncs_and_no_longer_fetches_updates(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(self.project),
                   "source": "startup", "model": "claude-sonnet-5-5"}
        state = notebook.load(payload)
        self.assertIsNone(brain.on_session_start(payload, state, backend))         # nothing shown to the agent
        paths = [r["path"] for r in self.server.requests]
        self.assertEqual(paths, ["/api/agents/register"])                          # no GET /api/updates
        self.assertEqual(self.read()["model"], "claude-sonnet-5-5")


# ---------------------------------------------------------------------------
# setup_identity.py (run by /breakpoint:setup)
# ---------------------------------------------------------------------------

class SetupScript(Isolated):
    def run_setup(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup_identity.main(list(args))
        return code, out.getvalue()

    def test_save_show_remove(self):
        code, out = self.run_setup("--email", "me@osu.edu", "--no-register")
        self.assertEqual(code, 0)
        self.assertIn("m***@osu.edu", out)
        self.assertNotIn("me@osu.edu", out)                                          # never printed in full
        self.assertEqual(self.read()["email"], "me@osu.edu")
        code, out = self.run_setup("--show")
        self.assertIn("Public name: agent-", out)
        self.assertNotIn(self.read()["install_id"], out)                             # full ID not printed
        code, out = self.run_setup("--remove-email", "--no-register")
        self.assertEqual(code, 0)
        self.assertNotIn("email", self.read())

    def test_bad_email(self):
        code, out = self.run_setup("--email", "nope", "--no-register")
        self.assertEqual(code, 2)
        self.assertIn("Not saved", out)

    def test_register_now_when_server_is_up(self):
        server = Recorder({("POST", "/api/agents/register"): (200, {"agent_id": "x"})})
        self.addCleanup(server.close)
        os.environ["BREAKPOINT_API_URL"] = server.url
        code, out = self.run_setup("--email", "me@osu.edu")
        self.assertIn("The server has the change.", out)
        self.assertEqual(server.requests[-1]["body"]["email"], "me@osu.edu")


# ---------------------------------------------------------------------------
# mcp/server.py: the accounts edits (with stand-ins for the mcp and httpx packages)
# ---------------------------------------------------------------------------

class FakeHttpxResponse:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)

    def json(self):
        return self._data


def load_server(routes, calls):
    """Import mcp/server.py with fake `mcp` and `httpx` modules. `routes`: {(method, path): (status, json)}."""
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class FastMCP:
        def __init__(self, name, **settings):
            pass

        def tool(self, *args, **kwargs):
            return lambda fn: fn

        def run(self):
            pass

    fastmcp.FastMCP = FastMCP
    httpx = types.ModuleType("httpx")
    httpx.HTTPError = type("HTTPError", (Exception,), {})

    def request(method, url, json=None, params=None, headers=None, timeout=None):
        path = urlparse(url).path
        calls.append({"method": method, "path": path, "body": json, "params": params, "headers": headers})
        status, data = routes.get((method, path), (404, {"detail": "not found"}))
        return FakeHttpxResponse(status, data)

    httpx.request = request
    fakes = {"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
             "mcp.server.fastmcp": fastmcp, "httpx": httpx}
    spec = importlib.util.spec_from_file_location("ao_mcp_server_under_test", PLUGIN / "mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, fakes):
        spec.loader.exec_module(module)
    return module


class McpServer(Isolated):
    def setUp(self):
        super().setUp()
        identity.update_config({"api_url": "http://server.test", "api_key": "file-key", "model": "claude-sonnet-5-5"})
        self.calls = []
        self.routes = {
            ("POST", "/api/agents/register"): (200, {"agent_id": "x"}),
            ("POST", "/api/posts"): (200, {"post_id": 7, "forum": "general", "tags": ["general"],
                                           "attached_to_open_question": False, "duplicate_of": None}),
        }
        self.server = load_server(self.routes, self.calls)

    def test_settings_come_from_the_shared_config_file(self):
        self.assertEqual((self.server.API_URL, self.server.API_KEY), ("http://server.test", "file-key"))

    def test_open_question_sends_project_and_identity_only(self):
        out = self.server.post_open_question("Prisma fails\nTried X", "why", "general", ["docker"])
        self.assertIn("#7", out)
        paths = [c["path"] for c in self.calls]
        self.assertEqual(paths, ["/api/agents/register", "/api/posts"])   # no /api/accounts/watch any more
        posted = self.calls[1]
        agent = identity.agent_id()
        self.assertEqual(posted["body"]["agent_id"], agent)
        self.assertEqual(posted["body"]["project"], identity.project_id())
        self.assertTrue(posted["body"]["author"].startswith("agent-"))
        self.assertNotIn(identity.ensure()["install_id"], posted["body"]["author"])
        self.assertEqual(posted["headers"]["X-Agent-Id"], agent)
        self.assertNotIn("X-Agent-Secret", posted["headers"])
        self.assertNotIn("X-Agent-Secret", self.calls[0]["headers"])        # registration has no secret either

    def test_solution_for_open_question_makes_no_extra_call(self):
        """The server emails the asker and visitors itself when it attaches the solution."""
        self.routes[("POST", "/api/posts")] = (200, {"post_id": 3, "forum": "general", "tags": ["general"],
                                                     "attached_to_open_question": True})
        out = self.server.post("desc", "why", "fix", "general", ["docker"], open_question_id=3)
        self.assertIn("attached to open question #3", out)
        self.assertEqual(self.calls[-1]["path"], "/api/posts")

    def test_unregistered_agent_still_posts_without_agent_id(self):
        del self.routes[("POST", "/api/agents/register")]                            # neither accounts router...
        self.server.post_open_question("Q", "why", "general", [])                   # ...nor /api/agents
        posted = [c for c in self.calls if c["path"] == "/api/posts"][0]
        self.assertNotIn("agent_id", posted["body"])
        self.assertIn("project", posted["body"])
        self.assertEqual([c["path"] for c in self.calls][:2], ["/api/agents/register", "/api/agents"])

    def test_post_sends_the_attempt_and_returns_revise_feedback_unsaved(self):
        feedback = "REVISE: The post was not saved yet (try 1 of at most 3).\n1) Say why it works.\nThen ... attempt=2."
        self.routes[("POST", "/api/posts")] = (200, {"revise": True, "feedback": feedback, "post_id": None})
        self.assertEqual(self.server.post("desc", "why", "fix", "general", ["docker"], open_question_id=3), feedback)
        posted = [c for c in self.calls if c["path"] == "/api/posts"][0]
        self.assertEqual(posted["body"]["attempt"], 1)
        self.assertNotIn("/api/accounts/solved", [c["path"] for c in self.calls])   # nothing was saved
        self.assertEqual(self.server.post_open_question("Q", "why", "general", [], attempt=2), feedback)
        self.assertEqual(self.calls[-1]["body"]["attempt"], 2)
        self.assertNotIn("/api/accounts/watch", [c["path"] for c in self.calls])

    def test_post_shows_quality_notes_and_a_possible_answer_is_not_announced(self):
        self.routes[("POST", "/api/posts")] = (200, {"post_id": 3, "forum": "general", "tags": ["general"],
                                                     "attached_to_open_question": True, "linked_by": "ai_check",
                                                     "quality_notes": ["Use 2 to 4 tags."]})
        out = self.server.post("desc", "why", "fix", "general", ["docker"])
        self.assertIn("possible answer", out)
        self.assertIn("Quality notes for next time: Use 2 to 4 tags.", out)
        self.assertNotIn("/api/accounts/solved", [c["path"] for c in self.calls])

    def test_ask_for_help_sends_a_full_case_file(self):
        self.routes[("POST", "/api/help")] = (200, {"message": "TEAM_STARTED: r1"})
        (self.project / "app.py").write_text("import prisma\n\ndef main():\n    raise SystemExit(1)\n", encoding="utf-8")
        (self.project / ".env").write_text("API_KEY=very-secret-value\n", encoding="utf-8")
        (self.project / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
        (self.project / "package.json").write_text(json.dumps({"dependencies": {"@prisma/client": "^5.0.0",
                                                                                "left-pad": "1.0.0"}}), encoding="utf-8")
        (self.project / "package-lock.json").write_text(json.dumps({"packages": {
            "node_modules/@prisma/client": {"version": "5.22.0"}, "node_modules/ms": {"version": "2.1.3"}}}),
            encoding="utf-8")
        (self.tmp / "outside.txt").write_text("not the project's", encoding="utf-8")
        out = self.server.ask_for_help(
            "Prisma fails at startup", "python app.py", 'File "app.py", line 4\nPrismaClientInitializationError',
            tried=["reinstalled @prisma/client -> same error", "cleared the cache"], fingerprint="abc123",
            files=["app.py", str(self.project / "app.py"), ".env", "logo.png", "../outside.txt", "missing.py"])
        self.assertIn("TEAM_STARTED", out)
        body = self.calls[-1]["body"]
        self.assertEqual(self.calls[-1]["path"], "/api/help")
        self.assertEqual(body["tried"], [{"attempt": "reinstalled @prisma/client", "result": "same error"},
                                         {"attempt": "cleared the cache", "result": ""}])
        self.assertEqual([f["path"] for f in body["files"]], ["app.py"])   # once; no secret, binary or outside file
        self.assertIn("raise SystemExit", body["files"][0]["excerpt"])
        self.assertIn("@prisma/client 5.22.0 (package-lock.json)", body["environment"])   # named in the error
        self.assertNotIn("left-pad", body["environment"])
        self.assertNotIn("ms 2.1.3", body["environment"])
        self.assertIn("OS:", body["environment"])
        self.assertEqual((body["model"], body["fingerprint"], body["check_command"]),
                         ("claude-sonnet-5-5", "abc123", "python app.py"))
        self.assertEqual(body["project"], identity.project_id())
        self.assertNotIn("very-secret-value", json.dumps(body))
        self.assertLess(len(json.dumps(body)), 30_000)

    def test_ask_for_help_diff_leaves_out_secret_files(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.routes[("POST", "/api/help")] = (200, {"message": "TEAM_STARTED: r2"})
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test"]
        (self.project / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.project / ".env").write_text("TOKEN=old\n", encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "start"]):
            subprocess.run(git + args, cwd=self.project, capture_output=True, check=True)
        (self.project / "app.py").write_text("x = 2\n", encoding="utf-8")
        (self.project / ".env").write_text("TOKEN=brand-new-secret\n", encoding="utf-8")
        self.server.ask_for_help("x is wrong", "", "", expected="x == 1", actual="x == 2")
        body = self.calls[-1]["body"]
        self.assertIn("+x = 2", body["diff"])
        self.assertNotIn("brand-new-secret", json.dumps(body))
        self.assertEqual((body["expected"], body["actual"], body["symptom"]), ("x == 1", "x == 2", "x == 2"))


if __name__ == "__main__":
    unittest.main()
