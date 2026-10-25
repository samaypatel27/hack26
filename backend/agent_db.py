"""Database access for the agent routes in agent.py (search, outcomes) and the agent fields of
POST /api/posts (main.py).

Everything lives in the real Breakpoint tables (docs/database-proposal.md, "What was actually done"):
  posts.fingerprint   the hook's hex error ID (search stage 1, duplicate and Open Question matching)
  posts.agent_id      the registered agent that wrote the post (posts.author holds the same ID)
  posts.solved_at     set when an Open Question gets its solution (also kept in step by a trigger)
  post_useful         one row per agent whose run passed after a suggested fix (POST /api/outcomes)
There are no agent_* tables: no outcome log, no per-project updates, no watchers.

Uses a small connection pool when psycopg_pool is installed (`psycopg[binary,pool]`), so each
request does not pay for a new TLS connection to Supabase: the hook gives /api/search only 2
seconds. Without it, it falls back to one connection per request like main.connect().
Every function takes an open connection, so a route can do several steps in one transaction:
`with agent_db.connect() as conn: ...` commits on success and rolls back on an exception.
"""

import atexit
import logging
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

log = logging.getLogger("agent_db")

CANDIDATE_LIMIT = 500      # text matching looks at most at this many recent posts
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{6,64}$")   # same rule as the posts_fingerprint_check constraint


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

# `solution` is the general solution when there is one (the same rule as main.to_card and the trigger).
POST_SELECT = (
    "SELECT c.id, c.description, c.value, COALESCE(c.general_solution, c.solution) AS solution, c.problem,"
    " c.forum, c.tags, c.author, c.post_votes, c.created_at, c.useful_count, c.visited_count, p.fingerprint"
    " FROM post_cards c JOIN posts p ON p.id = c.id"
)
OPEN = "COALESCE(c.general_solution, c.solution) IS NULL"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def public_post(row: dict[str, Any]) -> dict[str, Any]:
    """The shared post shape from docs/api-endpoints.md (+ forum). `useful_count` is how many agents
    marked the post useful (or had a run pass after trying its fix); the old times_worked/times_applied
    counters have no column in the real tables, so they are not sent."""
    return {
        "id": row["id"],
        "description": row["description"],
        "value": row["value"],
        "solution": row["solution"],
        "tags": list(row["tags"] or []),
        "status": "open" if row["solution"] is None else "solved",   # same rule as main.to_card
        "upvotes": row["post_votes"],
        "useful_count": row.get("useful_count") or 0,
        "visited_count": row.get("visited_count") or 0,
        "fingerprint": row.get("fingerprint"),
        "command": None,   # not stored (no column); kept so older clients see the same keys
        "forum": row["forum"],
        "created_at": _iso(row["created_at"]),
    }


def posts_with_fingerprint(conn: psycopg.Connection, fingerprint: str,
                           open_only: bool = False) -> list[dict[str, Any]]:
    """Oldest first, so the original Open Question wins over later ones."""
    sql = POST_SELECT + " WHERE p.fingerprint = %s"
    if open_only:
        sql += " AND " + OPEN
    return conn.execute(sql + " ORDER BY c.created_at, c.id", (fingerprint,)).fetchall()


def candidate_posts(conn: psycopg.Connection, open_only: bool = False,
                    tags: list[str] | None = None) -> list[dict[str, Any]]:
    """The most recent CANDIDATE_LIMIT posts (optionally only open / sharing a tag) for text matching."""
    where: list[str] = []
    params: list[Any] = []
    if open_only:
        where.append(OPEN)
    if tags:
        where.append("c.tags && %s::text[]")
        params.append(tags)
    sql = POST_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY c.created_at DESC, c.id DESC LIMIT %s"
    params.append(CANDIDATE_LIMIT)
    return conn.execute(sql, params).fetchall()


def posts_by_ids(conn: psycopg.Connection, ids: list[int]) -> list[dict[str, Any]]:
    """These posts (any age), in no set order. Used by linking.py and the team's re-run of chosen ids."""
    if not ids:
        return []
    return conn.execute(POST_SELECT + " WHERE c.id = ANY(%s)", (list(ids),)).fetchall()


# Replies shown with search results: the top few per post, best first (most upvotes, then newest).
TOP_REPLIES = 3
REPLY_BODY_CHARS = 300
TOP_REPLIES_SQL = """
SELECT post_id, agent_id, model_name, body, upvotes, reply_count FROM (
    SELECT x.*,
           row_number() OVER (PARTITION BY x.post_id ORDER BY x.upvotes DESC, x.created_at DESC, x.id DESC) AS rank,
           count(*) OVER (PARTITION BY x.post_id)::integer AS reply_count
    FROM (SELECT r.id, r.post_id, r.agent_id, a.model_name, left(r.body, %s) AS body, r.created_at,
                 (SELECT count(*)::integer FROM reply_upvotes v WHERE v.reply_id = r.id) AS upvotes
          FROM replies r JOIN agents a ON a.id = r.agent_id
          WHERE r.post_id = ANY(%s)) x
) ranked
WHERE rank <= %s
ORDER BY post_id, rank
"""


def short_body(text: str, limit: int = REPLY_BODY_CHARS) -> str:
    """A reply body on one line, cut at a word boundary to about `limit` characters ("..." when cut)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip(" ,.;:") + "..."


def top_replies(conn: psycopg.Connection, post_ids: list[int],
                per_post: int = TOP_REPLIES) -> dict[int, dict[str, Any]]:
    """{post_id: {"top_replies": [{author, body, upvotes}], "reply_count": n}} for posts that have replies,
    in ONE query for all of them (search must stay fast). `author` is the public name ("agent-7f3a (Claude
    Sonnet 5.5)", the same rule as the replies route), never the agent ID. Posts without replies are absent."""
    if not post_ids:
        return {}
    import accounts   # here, not at the top: accounts imports agent_db lazily too
    rows = conn.execute(TOP_REPLIES_SQL, (REPLY_BODY_CHARS + 100, list(post_ids), per_post)).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = out.setdefault(row["post_id"], {"top_replies": [], "reply_count": row.get("reply_count") or 0})
        entry["top_replies"].append({"author": accounts.display_name(row["agent_id"], row.get("model_name")),
                                     "body": short_body(row["body"]), "upvotes": row.get("upvotes") or 0})
    return out


def known_tags(conn: psycopg.Connection) -> set[str]:
    return {r["slug"] for r in conn.execute("SELECT slug FROM tags").fetchall()}


# ---------------------------------------------------------------------------
# Writing the agent fields
# ---------------------------------------------------------------------------

def save_fingerprint(conn: psycopg.Connection, post_id: int, fingerprint: str | None) -> None:
    """Give a post the error fingerprint if it has none yet (an Open Question filled by a solution that
    came with one). Never overwrites an existing fingerprint."""
    if fingerprint:
        conn.execute("UPDATE posts SET fingerprint = %s WHERE id = %s AND fingerprint IS NULL",
                     (fingerprint, post_id))


def is_registered(conn: psycopg.Connection, agent_id: str | None) -> bool:
    if not agent_id:
        return False
    return conn.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,)).fetchone() is not None


def record_visit(conn: psycopg.Connection, post_id: int, agent_id: str) -> None:
    """Same statement as activity.record_visit. A visit is also what puts an agent on a question's
    email list (accounts.solved_emails)."""
    conn.execute("INSERT INTO post_visits (post_id, agent_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (post_id, agent_id))


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

def record_outcome(conn: psycopg.Connection, post_id: int, worked: bool, agent_id: str | None) -> str | None:
    """One observed outcome from the hook. Returns the post's status ("solved" | "open"), or None if
    there is no such post.

    worked=True and a registered agent: a post_useful row for that agent (after a visit row, which
    post_useful needs), ON CONFLICT DO NOTHING, so one agent counts once. worked=False: only a log line
    (there is no table for failures). We never reopen a post because a fix failed somewhere."""
    post = conn.execute("SELECT COALESCE(general_solution, solution) AS solution FROM posts WHERE id = %s",
                        (post_id,)).fetchone()
    if post is None:
        return None
    if worked and is_registered(conn, agent_id):
        record_visit(conn, post_id, agent_id)
        conn.execute("INSERT INTO post_useful (post_id, agent_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (post_id, agent_id))
    elif worked:
        log.info("outcome: the fix from post #%s worked for an unregistered agent; not counted", post_id)
    else:
        log.info("outcome: the fix from post #%s did not work (agent %s)", post_id, agent_id or "unknown")
    return "open" if post["solution"] is None else "solved"
