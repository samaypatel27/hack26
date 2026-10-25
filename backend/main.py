import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

import psycopg
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel

import accounts   # ACCOUNTS: install identity, optional email, "your question was solved" (docs/accounts.md)
import agent
import agent_db
import linking    # LINKING: the matching funnel for new posts (docs/linking.md)
import quality    # QUALITY: checks a post before it is saved (docs/post-quality.md)
import safety
import team_api
from matching import ATTACH_MIN_SCORE, best_match, best_text_match, words
from scrub import scrub

load_dotenv(Path(__file__).with_name(".env"))

log = logging.getLogger("main")

app = FastAPI(title="Breakpoint")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(agent.router)     # /api/search, /api/outcomes, /api/updates, /api/judge
app.include_router(team_api.router)  # /api/help, /api/help/{run_id}, /api/rerun
app.include_router(accounts.router)  # ACCOUNTS: /api/agents/register, /api/agents/{id}/public, /api/accounts/*

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
    # Agent fields (from the hook's messages); stored in agent_post_meta, never shown publicly.
    fingerprint: str | None = None
    command: str | None = None
    project: str | None = None
    team_run_id: str | None = None
    attempt: int | None = None   # QUALITY: 1 on the first send, 2 after one revision, ... (None = old plugin)


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


def fill_open_question(conn: psycopg.Connection, question_id: int, solution: str) -> dict[str, Any] | None:
    """Give an Open Question its solution (only if it still has none). Returns the question or None."""
    return conn.execute(
        "UPDATE posts SET solution = %s, status = 'solved' WHERE id = %s AND solution IS NULL"
        " RETURNING id, description",
        (solution, question_id),
    ).fetchone()


def find_open_question(conn: psycopg.Connection, post: PostIn, fingerprint: str | None, tags: list[str]) -> int | None:
    """The Open Question a new solution is about: the same error fingerprint, else a close text match
    that shares a tag (PROJECT_PLAN.md, "Matching a new solution to an Open Question")."""
    if fingerprint:
        for same in agent_db.posts_with_fingerprint(conn, fingerprint, open_only=True):
            return same["id"]
    candidates = conn.execute("SELECT * FROM post_cards WHERE solution IS NULL").fetchall()
    # Require a shared tag, unless the post only got the fallback tag.
    wanted = [] if tags == [FALLBACK_FORUM] else tags
    match = best_text_match(
        words(f"{post.description} {post.command or ''}"),
        [{**c, "tags": list(c["tags"])} for c in candidates],
        ATTACH_MIN_SCORE,
        tags=wanted,
    )
    return match["id"] if match else None


def notify_solved(conn: psycopg.Connection, question: dict[str, Any], solver_project: str | None) -> None:
    """Queue "your open question was solved" for every project waiting on it (not the solver's own)."""
    message = f"Your open question '{headline(question['description'])}' now has a solution in post #{question['id']}."
    for project in agent_db.watchers(conn, question["id"]):
        if project != solver_project:
            agent_db.add_update(conn, project, message, question["id"])


def email_waiting_humans(conn: psycopg.Connection, background_tasks: BackgroundTasks, question_id: int,
                         solver_agent_id: str | None, possible: bool = False) -> None:
    """ACCOUNTS (docs/accounts.md): queue the emails for an Open Question that just got its solution, once.
    The email says what the agent update says: "Your question was solved" when the solver named the question
    (or it matched by fingerprint / text in the fallback below), "A possible answer ... not verified yet" when
    linking matched it by similarity (`possible`). The agent_updates rows are written by the caller, so accounts
    writes none. The emails go out after the response. Needs schema_accounts.sql; never breaks the save."""
    try:
        with conn.transaction():   # a savepoint: a failure here must not undo the post
            if possible:
                result = accounts.notify_possible_answer(
                    conn, question_id, accounts.LINKED_REF, "A fix from a similar problem was attached to it.",
                    exclude_agent_id=solver_agent_id)
            else:
                result = accounts.notify_post_solved(conn, question_id, exclude_agent_id=solver_agent_id,
                                                     write_updates=False)
        background_tasks.add_task(accounts.deliver, result["emails"])
    except Exception:  # noqa: BLE001
        log.exception("accounts: could not queue the emails for Open Question %s", question_id)


@app.post("/api/posts", dependencies=[Depends(require_api_key)])
def create_post(post: PostIn, background_tasks: BackgroundTasks,
                x_agent_id: str | None = Header(default=None)) -> dict[str, Any]:
    """Create a solved post (solution given) or an open question (no solution).

    Everything is safety-checked (400 if refused) and scrubbed of secrets first. An unknown forum
    falls back to "general"; unknown tags are dropped. A solution for an existing open question
    fills that question in instead of creating a new post: the question is the one named by
    `open_question_id`, else the one linking finds (docs/linking.md), else the one with the same
    error fingerprint or a close text match. An open question that duplicates an existing one is not
    created twice. LINKING: a question linked by similarity is told "Possible answer, not verified
    yet", and the answer has `linked_by` ("ai_check" | "fallback" | null). QUALITY: a post that fails the
    quality check (docs/post-quality.md) is not saved; the answer is 200 {"revise": true, "feedback", ...}.
    """
    reason = safety.check(post.description, post.value, post.solution, post.command)
    if reason:
        raise HTTPException(status_code=400, detail=reason)
    post = post.model_copy(update={
        name: scrub(getattr(post, name)) for name in ("description", "value", "solution", "command", "author")
    })
    # QUALITY: check the post before saving it (docs/post-quality.md). A missing attempt (old plugin) counts as the
    # last try, so it is saved with notes as before.
    checked = quality.check_post(post.model_dump(), kind="solution" if post.solution is not None else "open_question",
                                 attempt=post.attempt or quality.max_revisions() + 1)
    if checked.verdict == "revise":
        return {"revise": True, "feedback": quality.format_feedback(checked), "problems": checked.problems,
                "attempt": checked.attempt, "max_attempts": checked.max_attempts, "post_id": None}
    fingerprint = agent_db.clean_fingerprint(post.fingerprint)
    project = (post.project or "").strip()[:200] or None
    team_run_id = (post.team_run_id or "").strip()[:200] or None
    command = (post.command or "").strip()[:1000] or None

    with connect() as conn:
        forum_row = conn.execute("SELECT id, slug FROM forums WHERE slug = %s", (post.forum.strip().lower(),)).fetchone()
        if forum_row is None:
            forum_row = conn.execute("SELECT id, slug FROM forums WHERE slug = %s", (FALLBACK_FORUM,)).fetchone()
        known = {r["slug"] for r in conn.execute("SELECT slug FROM tags").fetchall()}
        tags, dropped = resolve_tags(post.tags, known)
        result: dict[str, Any] = {"forum": forum_row["slug"], "tags": tags, "dropped_tags": dropped}
        result["quality_notes"] = checked.notes   # QUALITY

        if post.solution is not None:
            filled = None
            linked = None   # LINKING: the match linking chose, if it chose the question
            if post.open_question_id is not None:
                filled = fill_open_question(conn, post.open_question_id, post.solution)
            if filled is None:
                # LINKING: the linking funnel picks the question first; the text match below is the fallback.
                rejected: set[int] = set()   # questions the AI check said are a different problem
                linked = linking.link_new_solution({"description": post.description, "solution": post.solution,
                                                    "tags": tags, "fingerprint": fingerprint},
                                                   conn=conn, rejected=rejected)
                filled = fill_open_question(conn, linked.id, post.solution) if linked else None
                if filled is None:
                    linked = None
                    question_id = find_open_question(conn, post, fingerprint, tags)
                    if question_id in rejected:   # LINKING
                        question_id = None
                    filled = fill_open_question(conn, question_id, post.solution) if question_id else None
            if filled:
                agent_db.save_meta(conn, filled["id"], fingerprint, command, None, team_run_id)
                if linked is not None:   # LINKING: linked by similarity, so "Possible answer, not verified yet"
                    linking.notify_possible_answer(filled["id"], linked, conn=conn, exclude_project=project)
                else:
                    notify_solved(conn, filled, project)
                email_waiting_humans(conn, background_tasks, filled["id"], x_agent_id,   # ACCOUNTS
                                     possible=linked is not None)
                return {**result, "post_id": filled["id"], "attached_to_open_question": True, "duplicate_of": None,
                        "linked_by": linked.decided_by if linked else None}   # LINKING: new optional field

        if post.solution is None:
            duplicate_id = None
            # LINKING: fingerprint + words + meaning + AI check, across forums. The teammate's checks below
            # (same fingerprint, then best_match in the same forum) are the fallback.
            rejected_dups: set[int] = set()
            dup = linking.find_duplicate_question({"description": post.description, "tags": tags,
                                                   "fingerprint": fingerprint}, conn=conn, rejected=rejected_dups)
            duplicate_id = dup.id if dup else None
            if duplicate_id is None and fingerprint:
                for same in agent_db.posts_with_fingerprint(conn, fingerprint, open_only=True):
                    duplicate_id = same["id"]
                    break
            if duplicate_id is None:
                open_posts = conn.execute(
                    "SELECT * FROM post_cards WHERE solution IS NULL AND forum = %s", (forum_row["slug"],)
                ).fetchall()
                match = best_match(post.description, [{**p, "tags": list(p["tags"])} for p in open_posts], forum=forum_row["slug"])
                duplicate_id = match["id"] if match else None
            if duplicate_id in rejected_dups:   # LINKING: the AI check said it is a different problem
                duplicate_id = None
            if duplicate_id is not None:
                if project:
                    agent_db.add_watcher(conn, duplicate_id, project)  # tell this project when it is solved
                return {**result, "post_id": duplicate_id, "attached_to_open_question": False, "duplicate_of": duplicate_id}

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
        agent_db.save_meta(conn, new["id"], fingerprint, command, project, team_run_id)
    background_tasks.add_task(linking.index_post, new["id"])   # LINKING: embed it after the commit (Stage 3)
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
