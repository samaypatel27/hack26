"""Backend: the hook's phone line to our server (PROJECT_PLAN.md, "Box 2: the API").

Standard library only (urllib). Every function returns None on ANY problem: server down, timeout,
bad JSON, a non-2xx answer. The hook must never freeze or crash the agent, so callers treat None
as "no answer" and carry on.

Where the server is (first match wins):
  1. env BREAKPOINT_API_URL / BREAKPOINT_API_KEY (also the old AGENTOVERFLOW_API_URL / _KEY, and API_URL / API_KEY,
     like the MCP server)
  2. ~/.breakpoint/config.json  {"api_url": ..., "api_key": ...}  (identity.py copies an old ~/.agentoverflow one over)
  3. http://localhost:8000 with no key

Contract (docs/hook-contracts.md; plugin/hook/dev/mock_backend.py implements it):
  GET  /api/search?error=&command=&fingerprint=&tags=a,b  -> [post, ...] best first, [] if none
  POST /api/outcomes {post_id, worked, fingerprint, command, session_id, source: "hook"} -> {"post_status"}
                     (the server counts worked=true as a "useful" mark for this agent, from X-Agent-Id)
  POST /api/judge    {session_id, actions, replies}       -> {"stuck", "reason", "confidence"}
  POST /api/agents/register {agent_id, model_name, email} -> {"agent_id", ...}  (accounts, docs/accounts.md)
  POST /api/help     the case file (case_builder.py), as the MCP `ask_for_help` tool sends it -> {"message", ...}
                     (only after the user picked "Call the specialist team" in the stuck menu, or in team_mode auto)

The team_mode setting (env BREAKPOINT_TEAM_MODE, else "team_mode" in the config file): what the hook does when the
agent is stuck and the forum has no fix. "ask" (default): the stuck menu for the user. "auto": start the team
right away. "off": never start or suggest the team.

Every call also carries the install's identity (identity.py): X-Agent-Id, X-Agent-Model, X-Project-Id.
There is no "updates" call any more: news about a solved question goes out by email (docs/accounts.md).

Speed: on Windows a connection to a closed local port waits for the full timeout, so after one
connection failure we remember "server down" in a temp file for DOWN_SECONDS and skip calls until then.
"""

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import identity   # accounts (docs/accounts.md): the install's agent ID, model and project

DEFAULT_URL = "http://localhost:8000"
CONFIG_PATH = identity.CONFIG_PATH   # ~/.breakpoint/config.json
SEARCH_TIMEOUT = 2.0
OUTCOME_TIMEOUT = 2.0
JUDGE_TIMEOUT = 5.0
REGISTER_TIMEOUT = 2.0
HELP_TIMEOUT = 10.0       # POST /api/help: the server's help checker may search before it starts a run
TEAM_MODES = ("ask", "auto", "off")
MAX_ERROR_CHARS = 2000
DOWN_SECONDS = 60

# Removed from everything we send (PROJECT_PLAN.md: scrub on the laptop and again on the server).
SECRET_PATTERNS = [
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "<secret>"),                 # sk-ant-..., sk-proj-...
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "<secret>"),                    # GitHub tokens
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<secret>"),                            # AWS access keys
    (re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}"), "<secret>"),          # JWTs
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2<secret>"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://<secret>@"),                      # user:pass@host
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _file_config() -> dict[str, Any]:
    return identity.read_config(CONFIG_PATH)   # {} on any problem; handles the old ~/.agentoverflow file


def _setting(env_names: list[str], file_key: str, default: str) -> str:
    for name in env_names:
        if os.environ.get(name):
            return os.environ[name]
    return str(_file_config().get(file_key) or default)


def api_url() -> str:
    url = _setting(["BREAKPOINT_API_URL", "AGENTOVERFLOW_API_URL", "API_URL"], "api_url", DEFAULT_URL).rstrip("/")
    # "localhost" makes urllib try IPv6 then IPv4, doubling the wait when the server is down.
    return url.replace("://localhost", "://127.0.0.1")


def api_key() -> str:
    return _setting(["BREAKPOINT_API_KEY", "AGENTOVERFLOW_API_KEY", "API_KEY"], "api_key", "")


def team_mode() -> str:
    """"ask", "auto" or "off". Anything else (an old "local" / "remote" value) counts as the default, "ask"."""
    value = _setting(["BREAKPOINT_TEAM_MODE"], "team_mode", "ask").strip().lower()
    return value if value in TEAM_MODES else "ask"


# ---------------------------------------------------------------------------
# Scrubbing and the "server is down" memory
# ---------------------------------------------------------------------------

def scrub(text: str) -> str:
    text = text or ""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    home = str(Path.home())
    if len(home) > 3:
        text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    return text


def _scrub_all(value: Any) -> Any:
    """scrub() every string inside a JSON-like value (the case file's lists and dicts too)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_all(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_all(v) for k, v in value.items()}
    return value


def _down_marker(url: str) -> Path:
    return Path(tempfile.gettempdir()) / f"breakpoint_down_{hashlib.sha1(url.encode()).hexdigest()[:8]}"


def _recently_down(url: str) -> bool:
    try:
        return time.time() - _down_marker(url).stat().st_mtime < DOWN_SECONDS
    except OSError:
        return False


def _mark_down(url: str, down: bool) -> None:
    try:
        if down:
            _down_marker(url).write_text(str(time.time()), encoding="utf-8")
        else:
            _down_marker(url).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# One HTTP call
# ---------------------------------------------------------------------------

def _request(method: str, path: str, timeout: float, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None, extra_headers: dict[str, str] | None = None) -> Any:
    """JSON from the server, or None on any problem."""
    base = api_url()
    if _recently_down(base):
        return None
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"X-API-Key": api_key(), "Accept": "application/json",
               **identity.headers(CONFIG_PATH), **(extra_headers or {})}   # accounts: who is asking
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    # Never send local calls through a system proxy.
    host = urllib.parse.urlparse(base).hostname or ""
    handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "::1", "localhost") else []
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                return None
            raw = resp.read()
        _mark_down(base, False)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        e.close()
        return None   # the server answered, it is not down
    except urllib.error.URLError:
        _mark_down(base, True)   # could not connect (refused, unreachable, connect timeout)
        return None
    except Exception:
        return None   # slow answer (read timeout), bad JSON, ...: no answer, but not "down"


# ---------------------------------------------------------------------------
# The calls the hook makes
# ---------------------------------------------------------------------------

def search(error: str, command: str, fingerprint: str | None, tags: list[str] | None = None) -> list[dict[str, Any]] | None:
    """Matching posts, best first ([] if none)."""
    params = {"error": scrub(error)[:MAX_ERROR_CHARS], "command": scrub(command), "fingerprint": fingerprint or ""}
    if tags:
        params["tags"] = ",".join(tags)
    data = _request("GET", "/api/search", SEARCH_TIMEOUT, params=params)
    if not isinstance(data, list):
        return None
    return [p for p in data if isinstance(p, dict)]


def report_outcome(post_id: int, worked: bool, fingerprint: str | None, command: str,
                   session_id: str) -> dict[str, Any] | None:
    """Tell the server a suggested fix worked or failed, from watching the command (never the agent's word)."""
    body = {"post_id": post_id, "worked": bool(worked), "fingerprint": fingerprint or "",
            "command": scrub(command), "session_id": session_id, "source": "hook"}
    data = _request("POST", "/api/outcomes", OUTCOME_TIMEOUT, body=body)
    return data if isinstance(data, dict) else None


def judge(session_id: str, actions: list[dict[str, Any]], replies: list[str]) -> dict[str, Any] | None:
    """Ask the AI judge (docs/struggle-signals.md) whether the agent is stuck."""
    body = {
        "session_id": session_id,
        "actions": [{k: scrub(v) if isinstance(v, str) else v for k, v in a.items()} for a in actions],
        "replies": [scrub(r) for r in replies],
    }
    data = _request("POST", "/api/judge", JUDGE_TIMEOUT, body=body)
    if not isinstance(data, dict) or "stuck" not in data:
        return None
    return data


def ask_help(case: dict[str, Any]) -> str | None:
    """Start the specialist team the way the MCP `ask_for_help` tool does: POST /api/help with the case file.
    The server's `message` (it starts with its tag: TEAM_STARTED, KNOWN_FIX, TRY_FIRST, ...), or None."""
    data = _request("POST", "/api/help", HELP_TIMEOUT, body=_scrub_all(case))
    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) and message.strip() else None


# ---------------------------------------------------------------------------
# Accounts (docs/accounts.md): register this install once, and again when the model or email changes
# ---------------------------------------------------------------------------

def sync_identity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """SessionStart: save the model Claude Code reported and the userConfig email, then register with the
    server if anything changed since the last time. Returns the server's answer, or None. Never raises."""
    try:
        identity.sync_from_session(payload, CONFIG_PATH)
        return register_agent()
    except Exception:
        return None


def register_agent(force: bool = False) -> dict[str, Any] | None:
    """POST /api/agents/register with this install's agent ID, model and email (or null to remove it)."""
    try:
        body = identity.registration_body(CONFIG_PATH)
        url = api_url()
        if not force and not identity.needs_registration(url, body, CONFIG_PATH):
            return {"agent_id": body["agent_id"], "unchanged": True}
        data = _request("POST", "/api/agents/register", REGISTER_TIMEOUT, body=body)
        if isinstance(data, dict) and data.get("agent_id"):
            identity.mark_registered(url, body, CONFIG_PATH)
            return data
        return None
    except Exception:
        return None
