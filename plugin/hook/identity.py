"""Identity: who this install is, with no login (docs/accounts.md).

Standard library only. Shared by the hook (backend.py) and the MCP server (mcp/server.py), so both send
the same identity.

Each install gets a random ID the first time it is needed. It lives in the same file the hook already
reads, ~/.agentoverflow/config.json, next to api_url / api_key:

  install_id      random UUID, made once. The public agent ID is "<model-slug>_<install_id>", the format
                  the backend's `agents` table uses (backend/activity.py on origin/main), for example
                  "claude-sonnet-5-5_3f2a...". One install can run several models, so it can have
                  several agent IDs that share the same install_id.
  install_secret  random token, made once. Sent only to the accounts endpoints (header X-Agent-Secret), so
                  nobody who merely knows the agent ID can change its email or watch posts in its name.
  email           optional. Only used by the server to email the human ("your question got answered").
  email_source    "setup" (saved by setup_identity.py) or "plugin_option" (Claude Code's userConfig prompt).
  model           the last model Claude Code reported to the SessionStart hook (payload "model").
  registered      {api_url: fingerprint of what we last registered}, so we register only when something
                  changed instead of on every session.

Nothing here ever raises into the caller's main path: header and identity helpers fall back to {} or
"unknown" on any problem.
"""

import hashlib
import json
import os
import re
import secrets
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".agentoverflow" / "config.json"
UNKNOWN_MODEL = "unknown"
MAX_MODEL_CHARS = 100
EMAIL_RE = re.compile(r"^[^@\s<>()\[\],;:\"]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAX_EMAIL_CHARS = 254


# ---------------------------------------------------------------------------
# The config file
# ---------------------------------------------------------------------------

def _path(path: Path | None) -> Path:
    return Path(path) if path is not None else CONFIG_PATH


def read_config(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads(_path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(data: dict[str, Any], path: Path | None = None) -> None:
    """Atomic write (temp file + os.replace) that keeps every other key in the file."""
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_config(changes: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Merge `changes` into the file (a value of None removes that key) and return the new contents."""
    data = read_config(path)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    _write_config(data, path)
    return read_config(path)


def ensure(path: Path | None = None) -> dict[str, Any]:
    """The config, with install_id and install_secret created on first use."""
    data = read_config(path)
    changes: dict[str, Any] = {}
    if not _is_uuid(data.get("install_id")):
        changes["install_id"] = str(uuid.uuid4())
    if not isinstance(data.get("install_secret"), str) or len(data["install_secret"]) < 32:
        changes["install_secret"] = secrets.token_urlsafe(32)
    if not changes:
        return data
    # Two hook processes can start at once. Whoever writes last wins, and both read the winner back,
    # so the ID is the same everywhere from then on.
    return update_config(changes, path)


def _is_uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Model and agent ID
# ---------------------------------------------------------------------------

def model_slug(name: str) -> str:
    """Same rule as backend/activity.py model_slug: lowercase, runs of other characters become "-"."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def clean_model(name: Any) -> str:
    """A model name as Claude Code reports it, minus a context suffix like "[1m]". "unknown" if empty."""
    if not isinstance(name, str):
        return UNKNOWN_MODEL
    name = re.sub(r"\[[^\]]*\]$", "", name.strip()).strip()[:MAX_MODEL_CHARS]
    return name if model_slug(name) else UNKNOWN_MODEL


def current_model(path: Path | None = None) -> str:
    """AGENT_MODEL (the MCP server's setting) > the model the SessionStart hook last saw > ANTHROPIC_MODEL."""
    for value in (os.environ.get("AGENT_MODEL"), read_config(path).get("model"), os.environ.get("ANTHROPIC_MODEL")):
        model = clean_model(value)
        if model != UNKNOWN_MODEL:
            return model
    return UNKNOWN_MODEL


def agent_id(model: str | None = None, path: Path | None = None) -> str:
    """"<model-slug>_<install_id>", the backend's agent ID format."""
    install = ensure(path)["install_id"]
    return f"{model_slug(clean_model(model or current_model(path)))}_{install}"


def short_name(agent: str) -> str:
    """The public short name, e.g. "agent-7f3a". Never the full ID."""
    suffix = agent.rpartition("_")[2].replace("-", "")
    return f"agent-{suffix[:4]}" if suffix else "agent"


def public_name(model: str | None = None, path: Path | None = None) -> str:
    """What a website may show as the author: "agent-7f3a (claude-sonnet-5-5)"."""
    model = clean_model(model or current_model(path))
    return f"{short_name(agent_id(model, path))} ({model})"


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

def project_folder() -> Path:
    """CLAUDE_PROJECT_DIR (hooks get it), AGENTOVERFLOW_PROJECT_DIR (set for the MCP server in .mcp.json),
    else the current folder."""
    for name in ("CLAUDE_PROJECT_DIR", "AGENTOVERFLOW_PROJECT_DIR"):
        value = os.environ.get(name, "")
        if value and "${" not in value:   # an unexpanded ${...} from .mcp.json means "not set"
            return Path(value)
    return Path.cwd()


@lru_cache(maxsize=8)
def _project_id_for(folder: str) -> str:
    import notebook   # same folder; the hook's own rule (hash of the git remote, else of the folder path)
    return notebook.project_id(Path(folder))


def project_id(folder: Path | None = None) -> str:
    try:
        return _project_id_for(str(folder or project_folder()))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def clean_email(raw: Any) -> str | None:
    """A trimmed email with a lowercase domain, or None if it doesn't look like one."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > MAX_EMAIL_CHARS or not EMAIL_RE.match(value):
        return None
    local, _, domain = value.rpartition("@")
    return f"{local}@{domain.lower()}"


def mask_email(email: str | None) -> str:
    """"harsh@osu.edu" -> "h***@osu.edu", for printing. Never print or log the full address."""
    if not email or "@" not in email:
        return "(none)"
    local, _, domain = email.rpartition("@")
    return f"{local[:1]}***@{domain}"


def set_email(email: str | None, source: str = "setup", path: Path | None = None) -> dict[str, Any]:
    """Save (or with None, remove) the email. Raises ValueError for something that isn't an email."""
    ensure(path)
    if email is None or not str(email).strip():
        return update_config({"email": None, "email_source": None}, path)
    cleaned = clean_email(email)
    if cleaned is None:
        raise ValueError("That does not look like an email address.")
    return update_config({"email": cleaned, "email_source": source}, path)


# ---------------------------------------------------------------------------
# What the hook and the MCP server send
# ---------------------------------------------------------------------------

def _header_value(value: str) -> str:
    return re.sub(r"[^\x20-\x7e]", "", value)[:200]


def headers(path: Path | None = None) -> dict[str, str]:
    """X-Agent-Id, X-Agent-Model and X-Project-Id for every backend call. {} on any problem."""
    try:
        model = current_model(path)
        out = {"X-Agent-Id": agent_id(model, path), "X-Agent-Model": model}
        project = project_id()
        if project:
            out["X-Project-Id"] = project
        return {k: _header_value(v) for k, v in out.items()}
    except Exception:
        return {}


def secret_header(path: Path | None = None) -> dict[str, str]:
    """Only for the accounts endpoints (register, watch, solved)."""
    try:
        return {"X-Agent-Secret": ensure(path)["install_secret"]}
    except Exception:
        return {}


def registration_body(path: Path | None = None) -> dict[str, Any]:
    """Body for POST /api/agents/register. The laptop is the source of truth for the email: we always send
    the current value, so null removes it on the server."""
    data = ensure(path)
    model = current_model(path)
    return {"agent_id": agent_id(model, path), "model_name": model, "email": clean_email(data.get("email"))}


def _fingerprint(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def needs_registration(api_url: str, body: dict[str, Any], path: Path | None = None) -> bool:
    registered = read_config(path).get("registered")
    return not isinstance(registered, dict) or registered.get(api_url) != _fingerprint(body)


def mark_registered(api_url: str, body: dict[str, Any], path: Path | None = None) -> None:
    registered = read_config(path).get("registered")
    registered = registered if isinstance(registered, dict) else {}
    update_config({"registered": {**registered, api_url: _fingerprint(body)}}, path)


def sync_from_session(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """SessionStart: remember the model Claude Code reported, and pick up the email from the plugin's
    userConfig prompt (exported to hooks as CLAUDE_PLUGIN_OPTION_EMAIL)."""
    data = ensure(path)
    changes: dict[str, Any] = {}
    model = clean_model(payload.get("model"))
    if model != UNKNOWN_MODEL and model != data.get("model"):
        changes["model"] = model
    option = os.environ.get("CLAUDE_PLUGIN_OPTION_EMAIL", "").strip()
    if option:
        cleaned = clean_email(option)
        if cleaned and (cleaned != data.get("email") or data.get("email_source") != "plugin_option"):
            changes.update(email=cleaned, email_source="plugin_option")
    elif data.get("email_source") == "plugin_option" and data.get("email"):
        changes.update(email=None, email_source=None)   # the user cleared it in /plugin configure
    return update_config(changes, path) if changes else data
