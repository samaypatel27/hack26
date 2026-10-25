"""Database access for the /v1 API.

Agent posts live in the Breakpoint tables (posts, post_tags, read through the post_cards view),
written with the same statements as POST /api/posts in main.py, so they appear on the website.
Agent-only fields live in the v1_ tables from schema_v1.sql.

Uses a small connection pool when psycopg_pool is installed (`psycopg[binary,pool]`), so each
request doesn't pay for a new TLS connection to Supabase: the hook gives /v1/search only 2
seconds. Without it, it falls back to one connection per request like main.connect().
Every function takes an open connection, so a route can do several steps in one transaction:
`with db_v1.connect() as conn: ...` commits on success and rolls back on an exception.
"""

import atexit
import os
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
MAX_TAGS = 4               # same as main.MAX_TAGS
FALLBACK_FORUM = "general" # same as main.FALLBACK_FORUM

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
    " FROM post_cards c LEFT JOIN v1_post_meta m ON m.post_id = c.id"
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


def get_post(conn: psycopg.Connection, post_id: int) -> dict[str, Any] | None:
    return conn.execute(POST_SELECT + " WHERE c.id = %s", (post_id,)).fetchone()


def lock_post(conn: psycopg.Connection, post_id: int) -> bool:
    """Lock a post row for this transaction. False if it doesn't exist."""
    return conn.execute("SELECT id FROM posts WHERE id = %s FOR UPDATE", (post_id,)).fetchone() is not None


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


def list_posts(conn: psycopg.Connection, status: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Newest first. Same filters as GET /api/posts: status by solution, q over text, tags and forum."""
    where: list[str] = []
    params: list[Any] = []
    if status == "solved":
        where.append("c.solution IS NOT NULL")
    elif status == "open":
        where.append("c.solution IS NULL")
    if q:
        where.append("(c.description ILIKE %s OR c.value ILIKE %s OR c.solution ILIKE %s"
                     " OR array_to_string(c.tags, ' ') ILIKE %s OR c.forum ILIKE %s)")
        params += [f"%{q}%"] * 5
    sql = POST_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY c.created_at DESC, c.id DESC LIMIT %s"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Writing posts (same statements as POST /api/posts in main.py)
# ---------------------------------------------------------------------------

def known_tags(conn: psycopg.Connection) -> set[str]:
    return {r["slug"] for r in conn.execute("SELECT slug FROM tags").fetchall()}


def resolve_tags(requested: list[str], known: set[str]) -> tuple[list[str], list[str]]:
    """Same rule as main.resolve_tags: known tags only, de-duplicated, at most MAX_TAGS.
    Returns (kept, dropped); with nothing valid left, the post gets the general tag."""
    kept: list[str] = []
    dropped: list[str] = []
    for raw in requested:
        slug = raw.strip().lower()
        if not slug:
            continue
        if slug in known and slug not in kept and len(kept) < MAX_TAGS:
            kept.append(slug)
        else:
            dropped.append(slug)
    return (kept or [FALLBACK_FORUM], dropped)


def resolve_forum(conn: psycopg.Connection, requested: str | None, tags: list[str]) -> dict[str, Any]:
    """{id, slug} of the requested forum, else the first tag that names a forum, else general."""
    for slug in [requested] + tags + [FALLBACK_FORUM]:
        if slug:
            row = conn.execute("SELECT id, slug FROM forums WHERE slug = %s", (slug.strip().lower(),)).fetchone()
            if row:
                return row
    # Same as seed.py: make sure the general forum exists.
    return conn.execute(
        "INSERT INTO forums (slug, name) VALUES (%s, 'General')"
        " ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id, slug",
        (FALLBACK_FORUM,),
    ).fetchone()


def insert_post(conn: psycopg.Connection, fields: dict[str, Any], forum_id: int) -> int:
    """A new post (solved, or an Open Question when solution is None) plus its v1 metadata."""
    solution = fields.get("solution")
    post_id = conn.execute(
        "INSERT INTO posts (description, value, solution, author, forum_id, status)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (fields["description"], fields["value"], solution, fields["author"], forum_id,
         "solved" if solution is not None else "open"),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO post_tags (post_id, tag_id) SELECT %s, id FROM tags WHERE slug = ANY(%s)",
        (post_id, fields["tags"]),
    )
    conn.execute(
        "INSERT INTO v1_post_meta (post_id, fingerprint, command, project, team_run_id)"
        " VALUES (%s, %s, %s, %s, %s)",
        (post_id, fields.get("fingerprint"), fields.get("command"), fields.get("project"),
         fields.get("team_run_id")),
    )
    return post_id


def fill_open_question(conn: psycopg.Connection, post_id: int, solution: str,
                       fingerprint: str | None, command: str | None) -> bool:
    """Fill in an Open Question's solution (same UPDATE as main.create_post). The question's
    description and value stay as the asker wrote them. False if it was already solved."""
    filled = conn.execute(
        "UPDATE posts SET solution = %s, status = 'solved' WHERE id = %s AND solution IS NULL RETURNING id",
        (solution, post_id),
    ).fetchone()
    if not filled:
        return False
    conn.execute(
        "INSERT INTO v1_post_meta (post_id, fingerprint, command) VALUES (%s, %s, %s)"
        " ON CONFLICT (post_id) DO UPDATE SET"
        " fingerprint = COALESCE(v1_post_meta.fingerprint, EXCLUDED.fingerprint),"
        " command = COALESCE(v1_post_meta.command, EXCLUDED.command)",
        (post_id, fingerprint, command),
    )
    return True


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

def record_outcome(conn: psycopg.Connection, post_id: int, worked: bool, fingerprint: str | None,
                   command: str | None, session_id: str | None, source: str | None) -> str | None:
    """Count one observed outcome. Returns the post's status ("solved" | "open"), or None if
    there is no such post. v1 never reopens a failing fix; the counts are there for when we do."""
    post = conn.execute("SELECT solution FROM posts WHERE id = %s", (post_id,)).fetchone()
    if post is None:
        return None
    conn.execute(
        "INSERT INTO v1_post_meta (post_id, times_applied, times_worked) VALUES (%s, 1, %s)"
        " ON CONFLICT (post_id) DO UPDATE SET times_applied = v1_post_meta.times_applied + 1,"
        " times_worked = v1_post_meta.times_worked + EXCLUDED.times_worked",
        (post_id, 1 if worked else 0),
    )
    conn.execute(
        "INSERT INTO v1_outcomes (post_id, worked, fingerprint, command, session_id, source)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (post_id, worked, fingerprint, command, session_id, source),
    )
    return "open" if post["solution"] is None else "solved"


# ---------------------------------------------------------------------------
# Watchers and updates
# ---------------------------------------------------------------------------

def add_watcher(conn: psycopg.Connection, post_id: int, project: str) -> None:
    conn.execute(
        "INSERT INTO v1_watchers (post_id, project) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (post_id, project),
    )


def watchers(conn: psycopg.Connection, post_id: int) -> list[str]:
    """Projects to tell about this post: the original asker plus everyone who asked the same question."""
    rows = conn.execute(
        "SELECT project FROM v1_post_meta WHERE post_id = %s AND project IS NOT NULL"
        " UNION SELECT project FROM v1_watchers WHERE post_id = %s",
        (post_id, post_id),
    ).fetchall()
    return sorted(r["project"] for r in rows)


def add_update(conn: psycopg.Connection, project: str, message: str, post_id: int | None) -> None:
    conn.execute(
        "INSERT INTO v1_updates (project, message, post_id) VALUES (%s, %s, %s)",
        (project, message, post_id),
    )


def take_updates(conn: psycopg.Connection, project: str) -> list[dict[str, Any]]:
    """Undelivered updates for a project, oldest first, marked delivered in the same statement."""
    rows = conn.execute(
        "UPDATE v1_updates SET delivered = true WHERE project = %s AND NOT delivered"
        " RETURNING id, message, post_id",
        (project,),
    ).fetchall()
    return [{"message": r["message"], "post_id": r["post_id"]} for r in sorted(rows, key=lambda r: r["id"])]
