import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

import psycopg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel

from matching import best_match

load_dotenv(Path(__file__).with_name(".env"))

app = FastAPI(title="Breakpoint")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SORTS = {
    "top": "post_votes DESC, created_at DESC, id DESC",
    "newest": "created_at DESC, id DESC",
}

VOTE_COLUMNS = {"post": "post_votes", "solution": "solution_votes"}
VOTE_LIMIT = 30  # votes per IP per window
VOTE_WINDOW_S = 60
_votes_by_ip: dict[str, deque[float]] = defaultdict(deque)

MAX_TAGS = 4
FALLBACK_FORUM = "general"


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def headline(description: str, length: int = 120) -> str:
    first = description.strip().splitlines()[0] if description.strip() else ""
    return first if len(first) <= length else first[:length].rstrip() + "…"


def body(description: str) -> str:
    """The problem text without its first line (the headline)."""
    lines = description.strip().splitlines()
    return "\n".join(lines[1:]).strip()


def preview(description: str, length: int = 280) -> str:
    text = " ".join(body(description).replace("`", "").split())
    return text if len(text) <= length else text[:length].rstrip() + "…"


def to_card(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p["id"],
        "headline": headline(p["description"]),
        "preview": preview(p["description"]),
        "value": p["value"],
        "forum": p["forum"],
        "forum_name": p["forum_name"],
        "tags": list(p["tags"]),
        "author": p["author"],
        "post_votes": p["post_votes"],
        "solution_votes": p["solution_votes"],
        # Open means nobody has posted a solution.
        "is_open": p["solution"] is None,
        # Aliases for clients/tests written against the status/upvotes shape.
        "upvotes": p["post_votes"],
        "status": "open" if p["solution"] is None else "solved",
        "created_at": p["created_at"].isoformat(),
    }


def resolve_tags(requested: list[str], known: set[str]) -> tuple[list[str], list[str]]:
    """Keep known tags (any forum's), de-duplicated, at most MAX_TAGS. Returns (kept, dropped).
    With nothing valid left, the post gets the general tag."""
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


def allow_vote(ip: str, now: float | None = None) -> bool:
    """Sliding-window rate limit: at most VOTE_LIMIT votes per IP per VOTE_WINDOW_S seconds."""
    now = time.monotonic() if now is None else now
    hits = _votes_by_ip[ip]
    while hits and now - hits[0] > VOTE_WINDOW_S:
        hits.popleft()
    if len(hits) >= VOTE_LIMIT:
        return False
    hits.append(now)
    return True


class VoteIn(BaseModel):
    target: Literal["post", "solution"]
    direction: Literal[1, -1]


class PostIn(BaseModel):
    description: str
    value: str
    solution: str | None = None  # None = an open question
    forum: str = FALLBACK_FORUM
    tags: list[str] = []
    author: str = "anonymous"
    open_question_id: int | None = None


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/forums")
def list_forums() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT slug, name, description, tags, post_count FROM forum_overview"
            " ORDER BY post_count DESC, slug"
        ).fetchall()
    return [{**r, "tags": list(r["tags"])} for r in rows]


@app.get("/api/posts")
def list_posts(
    filter: Literal["all", "solved", "open"] = "all",
    forum: str | None = None,
    tags: str | None = Query(default=None, description="Comma-separated; a post must have all of them"),
    sort: Literal["top", "newest"] = "newest",
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if forum:
        where.append("forum = %s")
        params.append(forum.lower())
    wanted = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]
    if wanted:
        where.append("tags @> %s::text[]")
        params.append(wanted)
    if filter == "solved":
        where.append("solution IS NOT NULL")
    elif filter == "open":
        where.append("solution IS NULL")
    if q:
        where.append(
            "(description ILIKE %s OR value ILIKE %s OR solution ILIKE %s"
            " OR array_to_string(tags, ' ') ILIKE %s OR forum ILIKE %s)"
        )
        params += [f"%{q}%"] * 5
    sql = "SELECT * FROM post_cards"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {SORTS[sort]} LIMIT %s"
    params.append(limit)

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [to_card(p) for p in rows]


@app.get("/api/posts/{post_id}")
def get_post(post_id: int) -> dict[str, Any]:
    with connect() as conn:
        p = conn.execute("SELECT * FROM post_cards WHERE id = %s", (post_id,)).fetchone()
    if p is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return {**to_card(p), "body": body(p["description"]), "solution": p["solution"]}


@app.post("/api/posts", dependencies=[Depends(require_api_key)])
def create_post(post: PostIn) -> dict[str, Any]:
    """Create a solved post (solution given) or an open question (no solution).

    An unknown forum falls back to "general"; unknown tags are dropped. A solution for an
    existing open question fills that question in instead of creating a new post.
    """
    # TODO: run scrub.py and safety.py on the text before storing.
    with connect() as conn:
        forum_row = conn.execute("SELECT id, slug FROM forums WHERE slug = %s", (post.forum.strip().lower(),)).fetchone()
        if forum_row is None:
            forum_row = conn.execute("SELECT id, slug FROM forums WHERE slug = %s", (FALLBACK_FORUM,)).fetchone()
        known = {r["slug"] for r in conn.execute("SELECT slug FROM tags").fetchall()}
        tags, dropped = resolve_tags(post.tags, known)
        result: dict[str, Any] = {"forum": forum_row["slug"], "tags": tags, "dropped_tags": dropped}

        if post.solution is not None and post.open_question_id is not None:
            filled = conn.execute(
                "UPDATE posts SET solution = %s, status = 'solved' WHERE id = %s AND solution IS NULL RETURNING id",
                (post.solution, post.open_question_id),
            ).fetchone()
            if filled:
                return {**result, "post_id": filled["id"], "attached_to_open_question": True, "duplicate_of": None}

        if post.solution is None:
            open_posts = conn.execute(
                "SELECT * FROM post_cards WHERE solution IS NULL AND forum = %s", (forum_row["slug"],)
            ).fetchall()
            match = best_match(post.description, [{**p, "tags": list(p["tags"])} for p in open_posts], forum=forum_row["slug"])
            if match:
                return {**result, "post_id": match["id"], "attached_to_open_question": False, "duplicate_of": match["id"]}

        new = conn.execute(
            "INSERT INTO posts (description, value, solution, author, forum_id, status)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                post.description,
                post.value,
                post.solution,
                post.author,
                forum_row["id"],
                "solved" if post.solution is not None else "open",
            ),
        ).fetchone()
        conn.execute(
            "INSERT INTO post_tags (post_id, tag_id) SELECT %s, id FROM tags WHERE slug = ANY(%s)",
            (new["id"], tags),
        )
    return {**result, "post_id": new["id"], "attached_to_open_question": False, "duplicate_of": None}


@app.post("/api/posts/{post_id}/vote")
def vote(post_id: int, vote: VoteIn, request: Request) -> dict[str, int]:
    ip = request.client.host if request.client else "unknown"
    if not allow_vote(ip):
        raise HTTPException(status_code=429, detail="Too many votes. Try again in a minute.")
    column = VOTE_COLUMNS[vote.target]  # whitelisted, safe to interpolate
    with connect() as conn:
        row = conn.execute(
            f"UPDATE posts SET {column} = {column} + %s"
            " WHERE id = %s AND (%s = 'post' OR solution IS NOT NULL)"
            " RETURNING post_votes, solution_votes",
            (vote.direction, post_id, vote.target),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Post not found, or it has no solution to vote on")
    return row
