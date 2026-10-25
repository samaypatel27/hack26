"""Database access for the agent routes in agent.py (search, outcomes, updates) and the agent
fields of POST /api/posts (main.py).

Posts live in the Breakpoint tables and are written only by POST /api/posts. These helpers read them
through the post_cards view and keep agent-only fields in the agent_ tables from schema_agent.sql.

Uses a small connection pool when psycopg_pool is installed (`psycopg[binary,pool]`), so each
request doesn't pay for a new TLS connection to Supabase: the hook gives /api/search only 2
seconds. Without it, it falls back to one connection per request like main.connect().
Every function takes an open connection, so a route can do several steps in one transaction:
`with agent_db.connect() as conn: ...` commits on success and rolls back on an exception.
"""

import atexit
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

try:
    from psycopg_pool import ConnectionPool
except ImportError:  # optional dependency
    ConnectionPool = None  # type: ignore[assignment,misc]

CANDIDATE_LIMIT = 500      # text matching looks at most at this many recent posts
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{6,64}$")


def clean_fingerprint(raw: Any) -> str | None:
    """The hook's hex error ID, or None if missing or not hex (then we match by text instead)."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value if FINGERPRINT_RE.match(value) else None

_pool: Any = None
_pool_lock = threading.Lock()


def _get_pool() -> Any:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                os.environ["DATABASE_URL"],
                min_size=1,
                max_size=int(os.environ.get("V1_DB_POOL_SIZE", "5")),
                # prepare_threshold=None: also works through Supabase's transaction pooler.
                kwargs={"row_factory": dict_row, "prepare_threshold": None},
                check=ConnectionPool.check_connection,
                open=True,
            )
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


atexit.register(close_pool)


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    if ConnectionPool is None:
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
            yield conn
        return
    with _get_pool().connection() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Reading posts
# ---------------------------------------------------------------------------

POST_SELECT = (
    "SELECT c.id, c.description, c.value, c.solution, c.forum, c.tags, c.author, c.post_votes,"
    " c.created_at, m.fingerprint, m.command, m.project,"
    " COALESCE(m.times_worked, 0) AS times_worked, COALESCE(m.times_applied, 0) AS times_applied"
    " FROM post_cards c LEFT JOIN agent_post_meta m ON m.post_id = c.id"
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def public_post(row: dict[str, Any]) -> dict[str, Any]:
    """The shared post shape from docs/api-endpoints.md (+ forum). Never includes the project."""
    return {
        "id": row["id"],
        "description": row["description"],
        "value": row["value"],
        "solution": row["solution"],
        "tags": list(row["tags"] or []),
        "status": "open" if row["solution"] is None else "solved",   # same rule as main.to_card
        "upvotes": row["post_votes"],
        "times_worked": row["times_worked"],
        "times_applied": row["times_applied"],
        "fingerprint": row["fingerprint"],
        "command": row["command"],
        "forum": row["forum"],
        "created_at": _iso(row["created_at"]),
    }


def posts_with_fingerprint(conn: psycopg.Connection, fingerprint: str,
                           open_only: bool = False) -> list[dict[str, Any]]:
    """Oldest first, so the original Open Question wins over later ones."""
    sql = POST_SELECT + " WHERE m.fingerprint = %s"
    if open_only:
        sql += " AND c.solution IS NULL"
    return conn.execute(sql + " ORDER BY c.created_at, c.id", (fingerprint,)).fetchall()


def candidate_posts(conn: psycopg.Connection, open_only: bool = False,
                    tags: list[str] | None = None) -> list[dict[str, Any]]:
    """The most recent CANDIDATE_LIMIT posts (optionally only open / sharing a tag) for text matching."""
    where: list[str] = []
    params: list[Any] = []
    if open_only:
        where.append("c.solution IS NULL")
    if tags:
        where.append("c.tags && %s::text[]")
        params.append(tags)
    sql = POST_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY c.created_at DESC, c.id DESC LIMIT %s"
    params.append(CANDIDATE_LIMIT)
    return conn.execute(sql, params).fetchall()


def known_tags(conn: psycopg.Connection) -> set[str]:
    return {r["slug"] for r in conn.execute("SELECT slug FROM tags").fetchall()}


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

def record_outcome(conn: psycopg.Connection, post_id: int, worked: bool, fingerprint: str | None,
                   command: str | None, session_id: str | None, source: str | None) -> str | None:
    """Count one observed outcome. Returns the post's status ("solved" | "open"), or None if
    there is no such post. we never reopen a failing fix; the counts are there for when we do."""
    post = conn.execute("SELECT solution FROM posts WHERE id = %s", (post_id,)).fetchone()
    if post is None:
        return None
    conn.execute(
        "INSERT INTO agent_post_meta (post_id, times_applied, times_worked) VALUES (%s, 1, %s)"
        " ON CONFLICT (post_id) DO UPDATE SET times_applied = agent_post_meta.times_applied + 1,"
        " times_worked = agent_post_meta.times_worked + EXCLUDED.times_worked",
        (post_id, 1 if worked else 0),
    )
    conn.execute(
        "INSERT INTO agent_outcomes (post_id, worked, fingerprint, command, session_id, source)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (post_id, worked, fingerprint, command, session_id, source),
    )
    return "open" if post["solution"] is None else "solved"


# ---------------------------------------------------------------------------
# Watchers and updates
# ---------------------------------------------------------------------------

def take_updates(conn: psycopg.Connection, project: str) -> list[dict[str, Any]]:
    """Undelivered updates for a project, oldest first, marked delivered in the same statement."""
    rows = conn.execute(
        "UPDATE agent_updates SET delivered = true WHERE project = %s AND NOT delivered"
        " RETURNING id, message, post_id",
        (project,),
    ).fetchall()
    return [{"message": r["message"], "post_id": r["post_id"]} for r in sorted(rows, key=lambda r: r["id"])]


def add_update(conn: psycopg.Connection, project: str, message: str, post_id: int | None) -> None:
    conn.execute("INSERT INTO agent_updates (project, message, post_id) VALUES (%s, %s, %s)",
                 (project, message, post_id))


# ---------------------------------------------------------------------------
# Agent fields of a post, and the projects waiting on an Open Question
# ---------------------------------------------------------------------------

def save_meta(conn: psycopg.Connection, post_id: int, fingerprint: str | None, command: str | None,
              project: str | None, team_run_id: str | None) -> None:
    """Store the agent-only fields of a post. Fields not given keep what the post already has."""
    if not any((fingerprint, command, project, team_run_id)):
        return
    conn.execute(
        "INSERT INTO agent_post_meta (post_id, fingerprint, command, project, team_run_id)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (post_id) DO UPDATE SET"
        " fingerprint = COALESCE(EXCLUDED.fingerprint, agent_post_meta.fingerprint),"
        " command = COALESCE(EXCLUDED.command, agent_post_meta.command),"
        " project = COALESCE(agent_post_meta.project, EXCLUDED.project),"
        " team_run_id = COALESCE(EXCLUDED.team_run_id, agent_post_meta.team_run_id)",
        (post_id, fingerprint, command, project, team_run_id),
    )


def add_watcher(conn: psycopg.Connection, post_id: int, project: str) -> None:
    """Remember a project that hit the same Open Question later, so it gets the update too."""
    conn.execute("INSERT INTO agent_watchers (post_id, project) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (post_id, project))


def watchers(conn: psycopg.Connection, post_id: int) -> list[str]:
    """Projects to tell when an Open Question is solved: the asker plus everyone attached later."""
    rows = conn.execute(
        "SELECT project FROM agent_post_meta WHERE post_id = %s AND project IS NOT NULL"
        " UNION SELECT project FROM agent_watchers WHERE post_id = %s",
        (post_id, post_id),
    ).fetchall()
    return sorted(r["project"] for r in rows)
