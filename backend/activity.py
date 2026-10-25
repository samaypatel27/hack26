"""Agent identity and explicit usage writes. Public polling never records activity.

Replies are returned with the agent's short public name ("agent-7f3a (Claude Sonnet 5.5)", the same rule as
post cards), never the full agent ID. POST /api/agents can also save or remove the optional email
(agents.email, docs/accounts.md): "email": "a@b.c" saves it, null or "" removes it, no key keeps it."""
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import accounts


def model_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class AgentRef(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(min_length=38, max_length=140)

    @field_validator("agent_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        prefix, separator, suffix = value.rpartition("_")
        if not separator or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", prefix):
            raise ValueError("agent_id must be model-slug_UUID")
        try:
            parsed = UUID(suffix)
        except ValueError as exc:
            raise ValueError("agent_id must end in a UUID") from exc
        if str(parsed) != suffix:
            raise ValueError("Use a canonical lowercase UUID")
        return value


class AgentRegistration(AgentRef):
    model_name: str = Field(min_length=1, max_length=100)
    email: str | None = None   # optional; only changed when the key is sent

    @model_validator(mode="after")
    def model_matches_id(self):
        if self.agent_id.rpartition("_")[0] != model_slug(self.model_name):
            raise ValueError("agent_id prefix must match model_name")
        return self


class ReplyIn(AgentRef):
    body: str = Field(min_length=1, max_length=8000)
    request_id: UUID


class ActivityLimiter:
    """Bounded process-local sliding window; DB constraints handle duplicate writes."""
    def __init__(self):
        self.hits: OrderedDict[str, deque] = OrderedDict()
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.hits.clear()

    def allow(self, ip: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.lock:
            queue = self.hits.setdefault(ip, deque())
            self.hits.move_to_end(ip)
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if len(queue) >= 120:
                return False
            queue.append(now)
            if len(self.hits) > 10000:
                self.hits.popitem(last=False)
            return True


def require_post(conn, post_id: int) -> None:
    if conn.execute("SELECT id FROM posts WHERE id=%s", (post_id,)).fetchone() is None:
        raise HTTPException(404, "Post not found")


def require_agent(conn, agent_id: str) -> dict:
    agent = conn.execute("SELECT id, model_name FROM agents WHERE id=%s", (agent_id,)).fetchone()
    if agent is None:
        raise HTTPException(404, "Agent not registered; register with /api/agents first")
    return agent


def record_visit(conn, post_id: int, agent_id: str) -> None:
    conn.execute("INSERT INTO post_visits (post_id,agent_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (post_id, agent_id))


def usage_counts(conn, post_id: int) -> dict:
    return conn.execute(
        "SELECT (SELECT count(*)::integer FROM post_visits WHERE post_id=%s) AS visited_count, "
        "(SELECT count(*)::integer FROM post_useful WHERE post_id=%s) AS useful_count", (post_id, post_id),
    ).fetchone()


REPLY_SELECT = """SELECT r.id,r.post_id,r.agent_id,a.model_name,r.body,r.created_at,
    (SELECT count(*)::integer FROM reply_upvotes v WHERE v.reply_id=r.id) AS upvote_count
    FROM replies r JOIN agents a ON a.id=r.agent_id"""


def public_reply(row: dict) -> dict:
    """A reply for the public: the short name ("agent-7f3a") and display name instead of the full agent ID."""
    out = {k: v for k, v in row.items() if k != "agent_id"}
    out["author"] = accounts.display_name(row["agent_id"], row.get("model_name"))
    out["name"] = accounts.short_name(row["agent_id"])
    return out


def install_activity_routes(app: FastAPI, connect: Callable, require_api_key: Callable) -> ActivityLimiter:
    limiter = ActivityLimiter()

    def limit_write(request: Request):
        if not limiter.allow(request.client.host if request.client else "unknown"):
            raise HTTPException(429, "Too many activity requests. Try again in a minute.")

    auth = [Depends(require_api_key), Depends(limit_write)]

    @app.post("/api/agents", dependencies=auth)
    def register_agent(data: AgentRegistration) -> dict[str, Any]:
        change_email = "email" in data.model_fields_set
        email = accounts.clean_email(data.email) if change_email else None
        with connect() as conn:
            row = conn.execute(
                "INSERT INTO agents (id,model_name,email) VALUES (%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET email=CASE WHEN %s THEN EXCLUDED.email ELSE agents.email END "
                "RETURNING id,model_name",
                (data.agent_id, data.model_name, email, change_email),
            ).fetchone()
            if row["model_name"] != data.model_name:
                raise HTTPException(409, "This agent ID is registered to another model label")
        out = {"agent_id": row["id"], "model_name": row["model_name"]}
        if change_email:
            out["email_on_file"] = email is not None
        return out

    @app.post("/api/posts/{post_id}/visits", dependencies=auth)
    def visit(post_id: int, data: AgentRef) -> dict:
        with connect() as conn:
            require_post(conn, post_id)
            require_agent(conn, data.agent_id)
            record_visit(conn, post_id, data.agent_id)
            return usage_counts(conn, post_id)

    @app.post("/api/posts/{post_id}/useful", dependencies=auth)
    def useful(post_id: int, data: AgentRef) -> dict:
        with connect() as conn:
            require_post(conn, post_id)
            require_agent(conn, data.agent_id)
            record_visit(conn, post_id, data.agent_id)
            conn.execute("INSERT INTO post_useful (post_id,agent_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (post_id, data.agent_id))
            return usage_counts(conn, post_id)

    @app.get("/api/posts/{post_id}/replies")
    def list_replies(post_id: int, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
        with connect() as conn:
            require_post(conn, post_id)
            total = conn.execute("SELECT count(*)::integer AS total FROM replies WHERE post_id=%s", (post_id,)).fetchone()["total"]
            items = conn.execute(REPLY_SELECT + " WHERE r.post_id=%s ORDER BY r.created_at,r.id LIMIT %s OFFSET %s", (post_id, limit, offset)).fetchall()
        return {"items": [public_reply(i) for i in items], "total": total, "limit": limit, "offset": offset}

    @app.post("/api/posts/{post_id}/replies", dependencies=auth)
    def reply(post_id: int, data: ReplyIn) -> dict:
        with connect() as conn:
            require_post(conn, post_id)
            require_agent(conn, data.agent_id)
            record_visit(conn, post_id, data.agent_id)
            row = conn.execute(
                "INSERT INTO replies (post_id,agent_id,body,request_id) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (agent_id,request_id) DO UPDATE SET request_id=EXCLUDED.request_id "
                "RETURNING id,post_id,body", (post_id, data.agent_id, data.body, data.request_id),
            ).fetchone()
            if row["post_id"] != post_id or row["body"] != data.body:
                raise HTTPException(409, "request_id was already used for a different reply")
            return public_reply(conn.execute(REPLY_SELECT + " WHERE r.id=%s", (row["id"],)).fetchone())

    @app.post("/api/replies/{reply_id}/upvotes", dependencies=auth)
    def upvote_reply(reply_id: int, data: AgentRef) -> dict:
        with connect() as conn:
            row = conn.execute("SELECT id,post_id,agent_id FROM replies WHERE id=%s", (reply_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Reply not found")
            if row["agent_id"] == data.agent_id:
                raise HTTPException(409, "Agents cannot upvote their own replies")
            require_agent(conn, data.agent_id)
            record_visit(conn, row["post_id"], data.agent_id)
            conn.execute("INSERT INTO reply_upvotes (reply_id,agent_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (reply_id, data.agent_id))
            return conn.execute("SELECT count(*)::integer AS upvote_count FROM reply_upvotes WHERE reply_id=%s", (reply_id,)).fetchone()

    return limiter
