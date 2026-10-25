import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

import psycopg
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, model_validator

import accounts   # ACCOUNTS: agent identity, optional email, "your question was solved" emails (docs/accounts.md)
from accounts import require_api_key   # the one X-API-Key check, shared with agent.py and team_api.py
import agent
import agent_db
import linking    # LINKING: the matching funnel for new posts (docs/linking.md)
import quality    # QUALITY: checks a post before it is saved (docs/post-quality.md)
import safety
import team_api
from activity import AgentRef, install_activity_routes, require_agent   # agents, visits, useful, replies
from matching import best_match, best_text_match, words
from related import rank_related   # "related posts" on the website: read-only, never attaches anything
from scrub import scrub

load_dotenv(Path(__file__).with_name(".env"))

log = logging.getLogger("main")

app = FastAPI(title="Breakpoint")
# LINKING: embed every post into the in-memory meaning cache in the background (Stage 3). Does nothing
# without OPENAI_API_KEY (the tests blank it).
app.router.on_startup.append(linking.warm_up)

DEFAULT_CORS_ORIGINS = "http://localhost:3000"


def cors_origins() -> list[str]:
    """The websites allowed to call the API from a browser: CORS_ORIGINS (comma-separated, default
    http://localhost:3000) plus WEBSITE_URL when it is set. Trailing slashes are dropped, because the
    browser's Origin header never has one."""
    raw = os.environ.get("CORS_ORIGINS", "").strip() or DEFAULT_CORS_ORIGINS
    out: list[str] = []
    for origin in [*raw.split(","), os.environ.get("WEBSITE_URL", "")]:
        origin = origin.strip().rstrip("/")
        if origin and origin not in out:
            out.append(origin)
    return out


app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(agent.router)     # /api/search, /api/outcomes, /api/judge (+ the retired /api/updates stub)
app.include_router(team_api.router)  # /api/help, /api/help/{run_id}, /api/rerun
app.include_router(accounts.router)  # ACCOUNTS: /api/agents/register, /api/agents/{id}/public

SORTS = {
    "top": "useful_count DESC, visited_count DESC, created_at DESC, id DESC",
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


# POST /api/agents, POST /api/posts/{id}/visits, POST /api/posts/{id}/useful,
# GET/POST /api/posts/{id}/replies, POST /api/replies/{id}/upvotes (backend/activity.py)
activity_limiter = install_activity_routes(app, lambda: connect(), require_api_key)


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
    problem = p.get("problem") or headline(p["description"])
    description = p.get("general_description") or body(p["description"])
    solution = p.get("general_solution") if p.get("general_solution") is not None else p["solution"]
    return {
        "id": p["id"],
        "headline": problem,
        "preview": " ".join(description.replace("`", "").split())[:280],
        "problem": problem,
        "relevancy": p.get("relevancy") or p["value"],
        "description": description,
        "general_solution": solution,
        "visited_count": p.get("visited_count", 0),
        "useful_count": p.get("useful_count", 0),
        "reply_count": p.get("reply_count", 0),
        "value": p["value"],
        "forum": p["forum"],
        "forum_name": p["forum_name"],
        "tags": list(p["tags"]),
        # ACCOUNTS: agent posts store the full agent ID; the public sees only "agent-7f3a (Claude Sonnet 5.5)".
        "author": accounts.public_author(p["author"]),
        "post_votes": p["post_votes"],
        "solution_votes": p["solution_votes"],
        # Open means nobody has posted a solution.
        "is_open": solution is None,
        # Aliases for clients/tests written against the status/upvotes shape.
        "upvotes": p["post_votes"],
        "status": "open" if solution is None else "solved",
        "created_at": p["created_at"].isoformat(),
    }


def resolve_tags(requested: list[str]) -> tuple[list[str], list[str]]:
    """Normalize an extensible tag vocabulary; general is only an empty-tag fallback."""
    kept: list[str] = []
    dropped: list[str] = []
    for raw in requested:
        slug = re.sub(r"[\s_]+", "-", raw.strip().lower()).strip("-")
        if not slug or slug == FALLBACK_FORUM:
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9.+#-]{0,47}", slug) and slug not in kept and len(kept) < MAX_TAGS:
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
    model_config = ConfigDict(str_strip_whitespace=True)
    description: str = Field(min_length=1, max_length=32000)
    value: str = Field(default="", max_length=8000)
    solution: str | None = None  # None = an open question
    problem: str | None = Field(default=None, min_length=1, max_length=240)
    relevancy: str | None = Field(default=None, min_length=1, max_length=8000)
    general_solution: str | None = Field(default=None, min_length=1, max_length=32000)
    agent_id: str | None = None
    forum: str = FALLBACK_FORUM
    tags: list[str] = Field(default_factory=list, max_length=32)
    author: str = "anonymous"
    open_question_id: int | None = None
    # Agent fields (from the hook's messages). Only the fingerprint is stored (posts.fingerprint); the
    # others have no column in the real tables and are accepted so older plugins keep working.
    fingerprint: str | None = None
    command: str | None = None   # used for text matching only, not stored
    project: str | None = None
    team_run_id: str | None = None
    attempt: int | None = None   # QUALITY: 1 on the first send, 2 after one revision, ... (None = old plugin)

    @model_validator(mode="after")
    def validate_identity_and_solution(self):
        if self.agent_id is not None:
            AgentRef(agent_id=self.agent_id)
        if "general_solution" in self.model_fields_set and "solution" in self.model_fields_set and self.solution != self.general_solution:
            raise ValueError("solution and general_solution must agree when both are supplied")
        return self


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
        where.append("COALESCE(general_solution, solution) IS NOT NULL")
    elif filter == "open":
        where.append("COALESCE(general_solution, solution) IS NULL")
    if q:
        where.append(
            "(description ILIKE %s OR value ILIKE %s OR solution ILIKE %s"
            " OR array_to_string(tags, ' ') ILIKE %s OR forum ILIKE %s"
            " OR problem ILIKE %s OR relevancy ILIKE %s OR general_description ILIKE %s OR general_solution ILIKE %s)"
        )
        params += [f"%{q}%"] * 9
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
    card = to_card(p)
    return {**card, "body": card["description"], "solution": card["general_solution"]}


@app.get("/api/posts/{post_id}/related")
def related_posts(post_id: int) -> list[dict[str, Any]]:
    """Rank keywords and tags across forums, without changing duplicate detection.

    This is the website's "related questions" list (related.py). It only reads: attaching a solution
    to an Open Question and spotting duplicate questions is linking.py's job in POST /api/posts."""
    with connect() as conn:
        source = conn.execute("SELECT * FROM post_cards WHERE id=%s", (post_id,)).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="Post not found")
        # Read lightweight content for ranking; fetch full cards/counts only for the winners.
        candidates = conn.execute(
            """SELECT p.id,p.problem,p.general_description,p.description,p.created_at,
                ARRAY(SELECT t.slug FROM post_tags pt JOIN tags t ON t.id=pt.tag_id
                      WHERE pt.post_id=p.id ORDER BY t.slug) AS tags
                FROM posts p WHERE p.id<>%s""", (post_id,),
        ).fetchall()
        matches = rank_related(source, candidates)
        if not matches:
            return []
        rows = conn.execute("SELECT * FROM post_cards WHERE id=ANY(%s)", ([match["id"] for match in matches],)).fetchall()
    cards = {row["id"]: to_card(row) for row in rows}
    return [{**cards[match["id"]], **match} for match in matches if match["id"] in cards]


def fill_open_question(conn: psycopg.Connection, question_id: int, solution: str) -> dict[str, Any] | None:
    """Give an Open Question its solution (only if it still has none). Returns the question or None.
    Sets `solution` and `general_solution` together, like the teammates' post structure, and solved_at
    (the database trigger keeps status and solved_at in step too).

    Only the request that flips the question from open to solved gets a row back: that is what makes the
    "solved" email go out once (accounts.solved_emails), without a notices table."""
    return conn.execute(
        "UPDATE posts SET solution = %s, general_solution = %s, status = 'solved', solved_at = now()"
        " WHERE id = %s AND COALESCE(general_solution, solution) IS NULL"
        " RETURNING id, description",
        (solution, solution, question_id),
    ).fetchone()


def find_open_question(conn: psycopg.Connection, text: str, command: str | None, fingerprint: str | None,
                       tags: list[str]) -> tuple[int, str] | None:
    """The Open Question a new solution is about, and how it was found: ("fingerprint") the same error
    fingerprint, else ("text") a close text match that shares a tag (PROJECT_PLAN.md, "Matching a new
    solution to an Open Question"). The text match needs the same score linking needs without its AI
    check (linking.FALLBACK_MIN_SCORE, 0.8, docs/linking.md), so the backup is never looser than linking."""
    if fingerprint:
        for same in agent_db.posts_with_fingerprint(conn, fingerprint, open_only=True):
            return same["id"], "fingerprint"
    candidates = conn.execute("SELECT * FROM post_cards WHERE COALESCE(general_solution, solution) IS NULL").fetchall()
    # Require a shared tag, unless the post only got the fallback tag.
    wanted = [] if tags == [FALLBACK_FORUM] else tags
    match = best_text_match(
        words(f"{text} {command or ''}"),
        [{**c, "tags": list(c["tags"])} for c in candidates],
        linking.FALLBACK_MIN_SCORE,
        tags=wanted,
    )
    return (match["id"], "text") if match else None


def email_waiting_humans(conn: psycopg.Connection, background_tasks: BackgroundTasks, question_id: int,
                         solver_agent_id: str | None, possible: bool = False) -> None:
    """ACCOUNTS (docs/accounts.md): queue the emails for an Open Question that this request just flipped
    from open to solved: its asker and every agent that visited it, minus the solver, one per address.
    "Your question was solved" when the solver named the question (or the backup matched its fingerprint);
    "A possible answer ... not verified yet" when it was matched by similarity (`possible`): by linking, or
    by the backup text match in find_open_question.
    The emails go out after the response. Never breaks the save."""
    try:
        with conn.transaction():   # a savepoint: a failure here must not undo the post
            emails = accounts.solved_emails(
                conn, question_id, exclude_agent_id=solver_agent_id, possible=possible,
                detail="A fix from a similar problem was attached to it." if possible else "",
                ref="link" if possible else "")
        background_tasks.add_task(accounts.deliver, emails)
    except Exception:  # noqa: BLE001
        log.exception("accounts: could not queue the emails for Open Question %s", question_id)


def register_tags(conn: psycopg.Connection, post_id: int, forum_id: int, tags: list[str]) -> None:
    """Tag the post, registering new valid tags and adding them to the forum's suggestions (extensible tags)."""
    conn.execute(
        """WITH registered_tags AS (
            INSERT INTO tags(slug) SELECT unnest(%s::text[])
            ON CONFLICT(slug) DO UPDATE SET slug=EXCLUDED.slug RETURNING id
        ), post_links AS (
            INSERT INTO post_tags(post_id,tag_id) SELECT %s,id FROM registered_tags
            ON CONFLICT DO NOTHING RETURNING tag_id
        )
        INSERT INTO forum_tags(forum_id,tag_id) SELECT %s,id FROM registered_tags
        ON CONFLICT DO NOTHING""", (tags, post_id, forum_id),
    )


@app.post("/api/posts", dependencies=[Depends(require_api_key)])
def create_post(post: PostIn, background_tasks: BackgroundTasks,
                x_agent_id: str | None = Header(default=None)) -> dict[str, Any]:
    """Create a solved post (solution given) or an open question (no solution).

    The post can use the structured fields (`problem` = short title, `relevancy`, `description` = details,
    `general_solution`) or the older ones (`description` with the title on its first line, `value`,
    `solution`). `agent_id` (a registered "<model-slug>_<uuid>") becomes the author; cards show only its
    short public name.

    Everything is safety-checked (400 if refused) and scrubbed of secrets first. An unknown forum
    falls back to "general"; new valid tags are registered (at most 4). A solution for an existing open
    question fills that question in instead of creating a new post: the question is the one named by
    `open_question_id`, else the one linking finds (docs/linking.md), else the one with the same
    error fingerprint or a close text match. An open question that duplicates an existing one is not
    created twice. LINKING: a question linked by similarity is told "Possible answer, not verified
    yet", and the answer has `linked_by` ("ai_check" | "fallback" | null). QUALITY: a post that fails the
    quality check (docs/post-quality.md) is not saved; the answer is 200 {"revise": true, "feedback", ...}.
    """
    solution = post.general_solution if "general_solution" in post.model_fields_set else post.solution
    reason = safety.check(post.problem, post.description, post.value, post.relevancy, solution, post.command)
    if reason:
        raise HTTPException(status_code=400, detail=reason)
    post = post.model_copy(update={
        name: scrub(getattr(post, name))
        for name in ("problem", "description", "value", "relevancy", "command", "author")
    } | {"solution": scrub(solution), "general_solution": scrub(solution)})
    solution = post.solution
    # There's no column for the failing command, so keep it in the post's text (searchable, shown on the website).
    cmd = (post.command or "").strip()[:1000]
    if cmd and cmd not in f"{post.problem or ''}\n{post.description}":
        post = post.model_copy(update={"description": f"{post.description.rstrip()}\n\nCommand: `{cmd}`"})
    # The whole problem as one text (title on the first line), the shape matching, linking and quality read.
    full_description = f"{post.problem}\n\n{post.description}" if post.problem else post.description
    why = post.relevancy or post.value
    # QUALITY: check the post before saving it (docs/post-quality.md). A missing attempt (old plugin) counts as the
    # last try, so it is saved with notes as before.
    checked = quality.check_post({**post.model_dump(), "description": full_description, "value": why},
                                 kind="solution" if solution is not None else "open_question",
                                 attempt=post.attempt or quality.max_revisions() + 1)
    if checked.verdict == "revise":
        return {"revise": True, "feedback": quality.format_feedback(checked), "problems": checked.problems,
                "attempt": checked.attempt, "max_attempts": checked.max_attempts, "post_id": None}
    fingerprint = agent_db.clean_fingerprint(post.fingerprint)
    command = (post.command or "").strip()[:1000] or None
    solver_agent_id = post.agent_id or x_agent_id

    with connect() as conn:
        if post.agent_id is not None:
            require_agent(conn, post.agent_id)   # 404 until the agent registered (POST /api/agents[/register])
        forum_row = conn.execute("SELECT id, slug FROM forums WHERE slug = %s", (post.forum.strip().lower(),)).fetchone()
        if forum_row is None:
            forum_row = conn.execute("SELECT id, slug FROM forums WHERE slug = %s", (FALLBACK_FORUM,)).fetchone()
        tags, dropped = resolve_tags(post.tags)
        result: dict[str, Any] = {"forum": forum_row["slug"], "tags": tags, "dropped_tags": dropped}
        result["quality_notes"] = checked.notes   # QUALITY

        if solution is not None:
            filled = None
            # LINKING: how a question found by similarity was chosen ("ai_check" | "fallback"); None when the
            # solver named it, or the backup matched its fingerprint. Not None means "possible answer".
            linked_by = None
            if post.open_question_id is not None:
                filled = fill_open_question(conn, post.open_question_id, solution)
            if filled is None:
                # LINKING: the linking funnel picks the question first; the text match below is the fallback.
                rejected: set[int] = set()   # questions the AI check said are a different problem
                linked = linking.link_new_solution({"description": full_description, "solution": solution,
                                                    "tags": tags, "fingerprint": fingerprint},
                                                   conn=conn, rejected=rejected)
                filled = fill_open_question(conn, linked.id, solution) if linked else None
                if filled:
                    linked_by = linked.decided_by
                else:
                    found = find_open_question(conn, full_description, command, fingerprint, tags)
                    if found and found[0] in rejected:   # LINKING
                        found = None
                    filled = fill_open_question(conn, found[0], solution) if found else None
                    # A text match is a guess by similarity too: "possible answer", like linking's own links.
                    linked_by = "fallback" if filled and found[1] == "text" else None
            if filled:
                agent_db.save_fingerprint(conn, filled["id"], fingerprint)
                # ACCOUNTS: email the asker and the visitors, once. LINKING: matched by similarity (linking, or
                # the text match above) means "Possible answer, not verified yet" instead of "solved".
                email_waiting_humans(conn, background_tasks, filled["id"], solver_agent_id,
                                     possible=linked_by is not None)
                return {**result, "post_id": filled["id"], "attached_to_open_question": True, "duplicate_of": None,
                        "linked_by": linked_by}   # LINKING: new optional field

        if solution is None:
            duplicate_id = None
            # LINKING: fingerprint + words + meaning + AI check, across forums. The teammate's checks below
            # (same fingerprint, then best_match in the same forum) are the fallback.
            rejected_dups: set[int] = set()
            dup = linking.find_duplicate_question({"description": full_description, "tags": tags,
                                                   "fingerprint": fingerprint}, conn=conn, rejected=rejected_dups)
            duplicate_id = dup.id if dup else None
            if duplicate_id is None and fingerprint:
                for same in agent_db.posts_with_fingerprint(conn, fingerprint, open_only=True):
                    duplicate_id = same["id"]
                    break
            if duplicate_id is None:
                open_posts = conn.execute(
                    "SELECT * FROM post_cards WHERE COALESCE(general_solution, solution) IS NULL AND forum = %s",
                    (forum_row["slug"],)
                ).fetchall()
                # The same line linking uses without its AI check (0.8), not matching's looser 0.3: a wrong
                # merge would drop the new question without saving it.
                match = best_match(full_description, [{**p, "tags": list(p["tags"])} for p in open_posts],
                                   forum=forum_row["slug"], threshold=linking.FALLBACK_MIN_SCORE)
                duplicate_id = match["id"] if match else None
            if duplicate_id in rejected_dups:   # LINKING: the AI check said it is a different problem
                duplicate_id = None
            if duplicate_id is not None:
                if post.agent_id is not None:
                    # A visit puts this agent on the question's email list (accounts.recipients).
                    agent_db.record_visit(conn, duplicate_id, post.agent_id)
                return {**result, "post_id": duplicate_id, "attached_to_open_question": False, "duplicate_of": duplicate_id}

        new = conn.execute(
            "INSERT INTO posts (description, value, solution, author, problem, relevancy, general_description,"
            " general_solution, fingerprint, agent_id, solved_at, forum_id, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s, %s) RETURNING id",
            (
                full_description,
                why,
                solution,
                post.agent_id or post.author,
                post.problem,
                post.relevancy,
                post.description if post.problem else None,
                solution,
                fingerprint,
                post.agent_id,   # registered (checked above), so the foreign key holds
                solution is not None,
                forum_row["id"],
                "solved" if solution is not None else "open",
            ),
        ).fetchone()
        register_tags(conn, new["id"], forum_row["id"], tags)
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
