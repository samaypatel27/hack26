"""Accounts without login: install identity, optional email, "your question was solved" (docs/accounts.md).

Builds on the teammate's `agents` table and agent ID format (backend/activity.py on origin/main:
"<model-slug>_<lowercase UUID>"). The UUID part is the install ID: one install can run several
models, so it can own several agent IDs. This file adds only what was missing:

  - the optional email, private, per install          (table accounts_installs)
  - which agent and project asked / wait on a post     (table accounts_post_links)
  - sending each notification once                     (table accounts_notices)
  - the email itself, through mailer.py (log only unless RESEND_API_KEY is set)

The SQL is in schema_accounts.sql. Nothing here creates or changes tables. Without those tables the
endpoints answer 503 with a clear message, and the rest of the backend works as before.

Endpoints (writes need X-API-Key, like every other write):
  POST /api/agents/register   {agent_id, model_name, email?}   + X-Agent-Secret
       Creates the agent (same insert as POST /api/agents) and the install. "email": "a@b.c" saves it,
       null or "" removes it, a missing key leaves it alone. Answers with a MASKED email only.
  GET  /api/agents/{agent_id}/public  -> {"name": "agent-7f3a", "model_name", "display_name"}   (public)
  POST /api/accounts/watch    {post_id, project?, role?}       + X-Agent-Id, X-Agent-Secret, X-Project-Id
       "asker" (default) or "watcher": tell me when this post gets a solution.
  POST /api/accounts/solved   {post_id}                        (+ X-Agent-Id of the solver, optional)
       If the post has a solution and was not announced yet: one agent_updates row per waiting project
       (the next SessionStart tells that project's agent) and one email per waiting human who gave one.

main.py includes this router next to the agent and team routers. POST /api/agents/register does not
clash with POST /api/agents (backend/activity.py on origin/main): they are different paths.

POST /api/posts (main.py) calls notify_post_solved() + deliver() itself when it fills an Open Question,
so the emails go out even if the plugin never calls /api/accounts/solved. A second call is harmless:
each notice is sent once (accounts_notices).
"""

import hashlib
import hmac
import logging
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, HTTPException

try:
    from . import mailer   # when the backend runs as a package (origin/main's main.py style)
except ImportError:
    import mailer          # when run from backend/ (uvicorn main:app, pytest)

log = logging.getLogger("accounts")

router = APIRouter(prefix="/api", tags=["accounts"])

DEFAULT_WEBSITE_URL = "http://localhost:3000"
POST_PATH = "/posts/{id}"            # frontend/app/posts/[id]/page.tsx on origin/main
TABLES = ("agents", "accounts_installs", "accounts_post_links", "accounts_notices")
MIN_SECRET_CHARS = 32
MAX_EMAIL_CHARS = 254
MAX_PROJECT_CHARS = 200
EMAIL_RE = re.compile(r"^[^@\s<>()\[\],;:\"]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
NOT_SET_UP = "Accounts are not set up on this server yet: apply backend/schema_accounts.sql."
UNSET = object()


# ---------------------------------------------------------------------------
# Database access and "are the tables there?"
# ---------------------------------------------------------------------------

@contextmanager
def connect() -> Iterator[Any]:
    """agent_db's pooled connection (commits on success, rolls back on an exception)."""
    import agent_db

    with agent_db.connect() as conn:
        yield conn


_warned = threading.Event()


def has_table(conn: Any, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{name}",)).fetchone()
    return bool(row and row["ok"])


def tables_ready(conn: Any) -> bool:
    """True when every accounts table (and the teammate's agents table) exists. Logs once if not."""
    missing = [t for t in TABLES if not has_table(conn, t)]
    if missing and not _warned.is_set():
        _warned.set()
        log.warning("accounts disabled, missing tables %s: apply backend/schema_accounts.sql "
                    "(after backend/migrations/20261024_post_usage.sql)", ", ".join(missing))
    return not missing


def _require_ready(conn: Any) -> None:
    if not tables_ready(conn):
        raise HTTPException(status_code=503, detail=NOT_SET_UP)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Same rule as agent.require_api_key (a copy, so this file imports neither agent.py nor main.py)."""
    expected = os.environ.get("API_KEY")
    if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Checking input
# ---------------------------------------------------------------------------

def model_slug(name: str) -> str:
    """Same rule as activity.model_slug."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def parse_agent_id(raw: Any) -> tuple[str, UUID] | None:
    """(model slug, install UUID) for a valid "<model-slug>_<lowercase UUID>", else None.
    Same rules as activity.AgentRef."""
    if not isinstance(raw, str) or not 38 <= len(raw.strip()) <= 140:
        return None
    prefix, sep, suffix = raw.strip().rpartition("_")
    if not sep or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", prefix):
        return None
    try:
        install = UUID(suffix)
    except ValueError:
        return None
    return (prefix, install) if str(install) == suffix else None


def clean_email(raw: Any) -> str | None:
    """A trimmed email with a lowercase domain. Raises 400 for something that isn't one."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str) or len(raw.strip()) > MAX_EMAIL_CHARS or not EMAIL_RE.match(raw.strip()):
        raise HTTPException(status_code=400, detail="`email` must be an email address, or null to remove it.")
    local, _, domain = raw.strip().rpartition("@")
    return f"{local}@{domain.lower()}"


def clean_project(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return re.sub(r"[^A-Za-z0-9_.:-]", "", raw.strip())[:MAX_PROJECT_CHARS] or None


def clean_post_id(raw: Any) -> int:
    if isinstance(raw, bool) or not (isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit())):
        raise HTTPException(status_code=400, detail="`post_id` must be an integer.")
    return int(raw)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _secret(x_agent_secret: str | None) -> str:
    if not x_agent_secret or len(x_agent_secret) < MIN_SECRET_CHARS:
        raise HTTPException(status_code=401, detail="X-Agent-Secret is missing or too short.")
    return x_agent_secret


def mask_email(email: str | None) -> str | None:
    return mailer.mask(email) if email else None


# ---------------------------------------------------------------------------
# What the public may see: a short name and the model, never the email or the full ID
# ---------------------------------------------------------------------------

def short_name(agent_id: str) -> str:
    parsed = parse_agent_id(agent_id)
    return f"agent-{parsed[1].hex[:4]}" if parsed else "agent"


def pretty_model(model: str) -> str:
    """"claude-sonnet-5-5" -> "Claude Sonnet 5.5". Names that already have capitals or spaces stay."""
    if not model or model != model.lower() or " " in model:
        return model or "unknown model"
    words, version = [], []
    for part in model.split("-"):
        if part.isdigit():
            version.append(part)
            continue
        if version:
            words.append(".".join(version))
            version = []
        words.append(part.upper() if part in ("gpt", "ai") else part.capitalize())
    if version:
        words.append(".".join(version))
    return " ".join(words)


def display_name(agent_id: str, model_name: str | None = None) -> str:
    """"agent-7f3a (Claude Sonnet 5.5)"."""
    parsed = parse_agent_id(agent_id)
    model = model_name or (parsed[0] if parsed else "")
    return f"{short_name(agent_id)} ({pretty_model(model)})" if model else short_name(agent_id)


def public_author(author: Any, model_name: str | None = None) -> Any:
    """For public post cards: posts.author holds a full agent ID for agent posts (main.py on origin/main
    stores `post.agent_id or post.author`). Shorten those; leave other authors ("anonymous") alone."""
    return display_name(author, model_name) if parse_agent_id(author) else author


# ---------------------------------------------------------------------------
# Registering an install
# ---------------------------------------------------------------------------

def _check_install(conn: Any, install: UUID, secret: str) -> dict[str, Any] | None:
    """The install row if the secret matches; 403 if it belongs to someone else; None if it is new."""
    row = conn.execute("SELECT install_id, secret_hash, email FROM accounts_installs WHERE install_id = %s",
                       (install,)).fetchone()
    if row is not None and not hmac.compare_digest(row["secret_hash"], hash_secret(secret)):
        raise HTTPException(status_code=403, detail="This agent ID belongs to another install.")
    return row


def register(conn: Any, agent_id: str, model_name: str, secret: str, email: Any = UNSET) -> dict[str, Any]:
    """Create or refresh the agent and its install. email: UNSET = keep, None = remove, str = save."""
    parsed = parse_agent_id(agent_id)
    if parsed is None:
        raise HTTPException(status_code=400, detail="`agent_id` must be <model-slug>_<lowercase UUID>.")
    if not isinstance(model_name, str) or not 1 <= len(model_name.strip()) <= 100:
        raise HTTPException(status_code=400, detail="`model_name` must be 1-100 characters.")
    model_name = model_name.strip()
    if parsed[0] != model_slug(model_name):
        raise HTTPException(status_code=400, detail="agent_id prefix must match model_name")
    install = parsed[1]
    row = conn.execute(   # same statement as activity.register_agent
        "INSERT INTO agents (id, model_name) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id RETURNING id, model_name",
        (agent_id, model_name),
    ).fetchone()
    if row["model_name"] != model_name:
        raise HTTPException(status_code=409, detail="This agent ID is registered to another model label")
    existing = _check_install(conn, install, secret)
    if existing is None:
        conn.execute(
            "INSERT INTO accounts_installs (install_id, secret_hash, email, email_updated_at)"
            " VALUES (%s, %s, %s, CASE WHEN %s::text IS NULL THEN NULL ELSE now() END)"
            " ON CONFLICT (install_id) DO NOTHING",
            (install, hash_secret(secret), None if email is UNSET else email,
             None if email is UNSET else email),
        )
        existing = _check_install(conn, install, secret)   # a parallel first register may have won
    elif email is not UNSET and email != existing["email"]:
        conn.execute("UPDATE accounts_installs SET email = %s, email_updated_at = now() WHERE install_id = %s",
                     (email, install))
    conn.execute("UPDATE accounts_installs SET last_seen_at = now() WHERE install_id = %s", (install,))
    saved = conn.execute("SELECT email FROM accounts_installs WHERE install_id = %s", (install,)).fetchone()
    return {"agent_id": agent_id, "model_name": model_name, "display_name": display_name(agent_id, model_name),
            "email_on_file": bool(saved and saved["email"]), "email": mask_email(saved and saved["email"])}


def verify_agent(conn: Any, agent_id: Any, secret: str) -> tuple[str, UUID]:
    """The agent and its install, if it is registered and the secret matches. 404 / 403 otherwise."""
    parsed = parse_agent_id(agent_id)
    if parsed is None:
        raise HTTPException(status_code=400, detail="X-Agent-Id must be <model-slug>_<lowercase UUID>.")
    agent = conn.execute("SELECT id FROM agents WHERE id = %s", (agent_id,)).fetchone()
    install = _check_install(conn, parsed[1], secret)
    if agent is None or install is None:
        raise HTTPException(status_code=404, detail="Agent not registered; call POST /api/agents/register first")
    return agent_id, parsed[1]


# ---------------------------------------------------------------------------
# Linking posts, and notifying when one is solved
# ---------------------------------------------------------------------------

def link_post(conn: Any, post_id: int, agent_id: str, install: UUID, project: str | None,
              role: str = "asker") -> None:
    """Remember that this agent (and project) asked or waits on the post. Also fills agent_watchers when that
    table exists, so code that reads agent_watchers sees the same projects."""
    if role not in ("asker", "watcher"):
        raise HTTPException(status_code=400, detail='`role` must be "asker" or "watcher".')
    if conn.execute("SELECT id FROM posts WHERE id = %s", (post_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Post not found")
    conn.execute(
        "INSERT INTO accounts_post_links (post_id, agent_id, install_id, project, role) VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (post_id, agent_id, role) DO UPDATE SET project = COALESCE(EXCLUDED.project,"
        " accounts_post_links.project)",
        (post_id, agent_id, install, project, role),
    )
    if project and has_table(conn, "agent_watchers"):
        conn.execute("INSERT INTO agent_watchers (post_id, project) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (post_id, project))


def website_link(post_id: int) -> str:
    base = (os.environ.get("WEBSITE_URL", "").strip() or DEFAULT_WEBSITE_URL).rstrip("/")
    return base + POST_PATH.format(id=post_id)


def _post(conn: Any, post_id: int) -> dict[str, Any] | None:
    """The post as a dict, whatever columns this schema has (problem/general_solution may not exist)."""
    row = conn.execute("SELECT to_jsonb(p) AS row FROM posts p WHERE p.id = %s", (post_id,)).fetchone()
    return row["row"] if row else None


def _title(post: dict[str, Any]) -> str:
    """The post's short title: `problem` (origin/main), else the first line of the description."""
    first_line = next(iter((post.get("description") or "").strip().splitlines()), "")
    text = (post.get("problem") or first_line or "your question").strip()
    return text if len(text) <= 120 else text[:119].rstrip() + "..."


def waiting_projects(conn: Any, post_id: int) -> list[str]:
    """Every project waiting on the post: our links, plus agent_post_meta / agent_watchers when they exist."""
    projects = {r["project"] for r in conn.execute(
        "SELECT DISTINCT project FROM accounts_post_links WHERE post_id = %s AND project IS NOT NULL",
        (post_id,)).fetchall()}
    if has_table(conn, "agent_post_meta"):
        projects |= {r["project"] for r in conn.execute(
            "SELECT project FROM agent_post_meta WHERE post_id = %s AND project IS NOT NULL", (post_id,)).fetchall()}
    if has_table(conn, "agent_watchers"):
        projects |= {r["project"] for r in conn.execute(
            "SELECT project FROM agent_watchers WHERE post_id = %s", (post_id,)).fetchall()}
    return sorted(p for p in projects if p)


def waiting_emails(conn: Any, post_id: int, post: dict[str, Any], exclude_install: UUID | None) -> list[str]:
    """Emails of the humans waiting on the post (askers, watchers, and the post's author if it is an agent)."""
    installs = {r["install_id"] for r in conn.execute(
        "SELECT DISTINCT install_id FROM accounts_post_links WHERE post_id = %s", (post_id,)).fetchall()}
    author = parse_agent_id(post.get("author"))
    if author:
        installs.add(author[1])
    installs.discard(exclude_install)
    if not installs:
        return []
    rows = conn.execute("SELECT DISTINCT email FROM accounts_installs WHERE install_id = ANY(%s)"
                        " AND email IS NOT NULL ORDER BY email", (list(installs),)).fetchall()
    return [r["email"] for r in rows]


def notify_watchers(conn: Any, post_id: int, kind: str, update_message: str, subject: str, email_text: str,
                    ref: str = "", exclude_agent_id: str | None = None,
                    write_updates: bool = True) -> dict[str, Any]:
    """Queue one notification, once: agent_updates rows for the waiting projects (when write_updates and the
    table exists) and the emails to send. Returns {"status", "projects", "emails": [mailer.Email]}.
    Send the emails AFTER the transaction commits, with deliver() (e.g. FastAPI BackgroundTasks)."""
    if not tables_ready(conn):
        return {"status": "accounts_not_set_up", "projects": 0, "emails": []}
    post = _post(conn, post_id)
    if post is None:
        return {"status": "not_found", "projects": 0, "emails": []}
    claimed = conn.execute(
        "INSERT INTO accounts_notices (post_id, kind, ref) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING post_id",
        (post_id, kind, ref),
    ).fetchone()
    if claimed is None:
        return {"status": "already_sent", "projects": 0, "emails": []}
    projects = waiting_projects(conn, post_id) if write_updates and has_table(conn, "agent_updates") else []
    for project in projects:
        conn.execute("INSERT INTO agent_updates (project, message, post_id) VALUES (%s, %s, %s)",
                     (project, update_message, post_id))
    excluded = parse_agent_id(exclude_agent_id)
    emails = [mailer.Email(to=address, subject=subject, text=email_text,
                           idempotency_key=f"bp-{kind}-{post_id}-{ref}-{hash_secret(address)[:16]}")
              for address in waiting_emails(conn, post_id, post, excluded[1] if excluded else None)]
    conn.execute("UPDATE accounts_notices SET projects = %s, emails = %s WHERE post_id = %s AND kind = %s AND ref = %s",
                 (len(projects), len(emails), post_id, kind, ref))
    return {"status": "queued", "projects": len(projects), "emails": emails}


def notify_post_solved(conn: Any, post_id: int, exclude_agent_id: str | None = None,
                       write_updates: bool = True) -> dict[str, Any]:
    """An Open Question now has a solution: tell the waiting projects and humans, once.
    Pass write_updates=False if the caller already wrote its own agent_updates rows for this solve."""
    if not tables_ready(conn):
        return {"status": "accounts_not_set_up", "projects": 0, "emails": []}
    post = _post(conn, post_id)
    if post is None:
        return {"status": "not_found", "projects": 0, "emails": []}
    if not (post.get("general_solution") or post.get("solution")):
        return {"status": "not_solved", "projects": 0, "emails": []}
    title, link = _title(post), website_link(post_id)
    update = (f"Your Open Question #{post_id} was solved: {title}. Read the fix with get_post({post_id}), "
              f"apply it, and rerun the failing command to check it here.")
    text = (f"Good news: the question your coding agent asked was answered.\n\n"
            f"  #{post_id}: {title}\n\n"
            f"Read the answer: {link}\n\n"
            f"Your agent will also hear about it the next time you open that project in Claude Code.\n\n"
            f"You get this email because you added your address in the AgentOverflow plugin setup.\n"
            f"To stop, run /agentoverflow:setup remove in Claude Code.")
    return notify_watchers(conn, post_id, "solved", update, f"Your question #{post_id} was answered", text,
                           exclude_agent_id=exclude_agent_id, write_updates=write_updates)


def notify_possible_answer(conn: Any, post_id: int, ref: str, detail: str,
                           write_updates: bool = False) -> dict[str, Any]:
    """A possible answer (not verified yet) for an Open Question, for example from a cold-case re-run
    (backend/team_api.py, ref = the team run id). Emails the waiting humans once per ref. By default it
    writes no agent_updates rows, because the caller writes its own."""
    if not tables_ready(conn):
        return {"status": "accounts_not_set_up", "projects": 0, "emails": []}
    post = _post(conn, post_id)
    if post is None:
        return {"status": "not_found", "projects": 0, "emails": []}
    title, link = _title(post), website_link(post_id)
    update = f"Possible answer for your Open Question #{post_id}, not verified yet: {detail}"
    text = (f"A possible answer was found for the question your coding agent asked.\n\n"
            f"  #{post_id}: {title}\n\n"
            f"{detail}\n\n"
            f"It is not verified yet. Your agent will hear about it the next time you open that project in "
            f"Claude Code, and it can try the fix there.\n\n"
            f"The question: {link}\n\n"
            f"You get this email because you added your address in the AgentOverflow plugin setup.\n"
            f"To stop, run /agentoverflow:setup remove in Claude Code.")
    return notify_watchers(conn, post_id, "possible_answer", update, f"A possible answer for your question #{post_id}",
                           text, ref=ref, write_updates=write_updates)


def deliver(emails: list[mailer.Email], provider: mailer.Mailer | None = None) -> int:
    """Send queued emails through the provider (log-only unless RESEND_API_KEY is set). Never raises.
    Returns how many the provider accepted."""
    if not emails:
        return 0
    provider = provider or mailer.get_mailer()
    sent = 0
    for email in emails:
        try:
            sent += bool(provider.send(email))
        except Exception:  # noqa: BLE001
            log.exception("email provider %s failed", getattr(provider, "name", "?"))
    return sent


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _as_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    return raw


@router.post("/agents/register", dependencies=[Depends(require_api_key)])
def register_endpoint(raw: Any = Body(default=None),
                      x_agent_secret: str | None = Header(default=None)) -> dict[str, Any]:
    body = _as_object(raw)
    secret = _secret(x_agent_secret)
    email = clean_email(body.get("email")) if "email" in body else UNSET
    with connect() as conn:
        _require_ready(conn)
        return register(conn, body.get("agent_id"), body.get("model_name"), secret, email)


@router.get("/agents/{agent_id}/public")
def public_agent(agent_id: str) -> dict[str, Any]:
    """Public profile: short name and model only."""
    with connect() as conn:
        if not has_table(conn, "agents"):
            raise HTTPException(status_code=503, detail=NOT_SET_UP)
        row = conn.execute("SELECT id, model_name FROM agents WHERE id = %s", (agent_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"name": short_name(row["id"]), "model_name": row["model_name"],
            "display_name": display_name(row["id"], row["model_name"])}


@router.post("/accounts/watch", dependencies=[Depends(require_api_key)])
def watch_endpoint(raw: Any = Body(default=None), x_agent_id: str | None = Header(default=None),
                   x_agent_secret: str | None = Header(default=None),
                   x_project_id: str | None = Header(default=None)) -> dict[str, Any]:
    body = _as_object(raw)
    post_id = clean_post_id(body.get("post_id"))
    secret = _secret(x_agent_secret)
    project = clean_project(body.get("project")) or clean_project(x_project_id)
    role = body.get("role") or "asker"
    with connect() as conn:
        _require_ready(conn)
        agent, install = verify_agent(conn, body.get("agent_id") or x_agent_id, secret)
        link_post(conn, post_id, agent, install, project, role)
    return {"post_id": post_id, "watching": True, "role": role}


@router.post("/accounts/solved", dependencies=[Depends(require_api_key)])
def solved_endpoint(background: BackgroundTasks, raw: Any = Body(default=None),
                    x_agent_id: str | None = Header(default=None)) -> dict[str, Any]:
    body = _as_object(raw)
    post_id = clean_post_id(body.get("post_id"))
    with connect() as conn:
        _require_ready(conn)
        result = notify_post_solved(conn, post_id, exclude_agent_id=x_agent_id)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Post not found")
    background.add_task(deliver, result["emails"])   # after the commit, outside the request
    return {"post_id": post_id, "status": result["status"], "projects": result["projects"],
            "emails": len(result["emails"])}
