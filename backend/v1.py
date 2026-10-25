"""The /v1 API: endpoints 1-8 of docs/api-endpoints.md.

Callers: the hook (plugin/hook/backend.py: search, outcomes, updates, judge), the MCP tools
(plugin/mcp/server.py: posts, open-questions, get post, updates) and the website (post list).

Include it in main.py with one line:
    from v1 import router as v1_router; app.include_router(v1_router)

Rules for every endpoint:
  - Write endpoints (POST) need X-API-Key = env API_KEY. GETs are public.
  - Rejected input -> 400 {"detail": "<reason>"}; unknown ids -> 404.
  - Unknown request fields are ignored, so callers can add fields later.
  - Everything agents send is scrubbed (scrub.py); posts and open questions also go through the
    safety filter (safety.py) before anything is stored.
  - Posts are stored in the Breakpoint `posts` table like POST /api/posts does (forum fallback
    "general", known tags only, max 4), plus agent-only fields in v1_post_meta (db_v1.py).
"""

import hmac
import os
import re
from typing import Any

import psycopg
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

import db_v1 as db
import judge
import matching_v1 as matching
import safety
from scrub import scrub

router = APIRouter(prefix="/v1", tags=["v1"])

MAX_SEARCH_RESULTS = 10
MAX_QUERY_CHARS = 5000
LIMITS = {"description": 20000, "value": 5000, "solution": 20000, "command": 1000, "author": 64}
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{6,64}$")
FILTERS = {"all": None, "solved": "solved", "open": "open"}


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Same rule as main.require_api_key (a copy, so v1.py never imports main.py)."""
    expected = os.environ.get("API_KEY")
    if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Input cleaning
# ---------------------------------------------------------------------------

def bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def as_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise bad_request("Body must be a JSON object.")
    return raw


def require_fields(body: dict[str, Any], fields: list[str]) -> None:
    missing = [f for f in fields if not isinstance(body.get(f), str) or not body[f].strip()]
    if missing:
        raise bad_request(f"Missing required fields: {', '.join(missing)}")


def text_field(body: dict[str, Any], name: str) -> str | None:
    """A stripped, length-checked string field, or None when absent/empty."""
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise bad_request(f"`{name}` must be a string.")
    value = value.strip()
    if len(value) > LIMITS.get(name, 200):
        raise bad_request(f"`{name}` is too long (max {LIMITS.get(name, 200)} characters).")
    return value or None


def requested_tags(raw: Any) -> list[str]:
    """Tags as sent (a list, or a comma-separated string), lowercased. db.resolve_tags picks the valid ones."""
    items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [t.strip().lower() for t in items if isinstance(t, str) and t.strip()]


def clean_fingerprint(raw: Any) -> str | None:
    """The hook's hex error ID, or None if missing or not hex (then we match by text instead)."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value if FINGERPRINT_RE.match(value) else None


def clean_id(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def clean_post(body: dict[str, Any]) -> dict[str, Any]:
    """Validated, safety-checked and scrubbed fields for a post or an Open Question."""
    texts = {name: text_field(body, name) for name in ("description", "value", "solution", "command", "author")}
    reason = safety.check(*texts.values())
    if reason:
        raise bad_request(reason)
    fields: dict[str, Any] = {name: scrub(value) for name, value in texts.items()}
    fields["value"] = fields["value"] or ""
    fields["author"] = fields["author"] or "anonymous"     # main.PostIn default
    fields["forum"] = text_field(body, "forum")
    fields["requested_tags"] = requested_tags(body.get("tags"))
    fields["fingerprint"] = clean_fingerprint(body.get("fingerprint"))
    fields["project"] = text_field(body, "project")
    fields["team_run_id"] = text_field(body, "team_run_id")
    return fields


def place(conn: psycopg.Connection, fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve tags and forum like POST /api/posts. Returns (forum row, response extras)."""
    tags, dropped = db.resolve_tags(fields["requested_tags"], db.known_tags(conn))
    fields["tags"] = tags
    forum = db.resolve_forum(conn, fields["forum"], tags)
    return forum, {"forum": forum["slug"], "tags": tags, "dropped_tags": dropped}


def headline(description: str, length: int = 80) -> str:
    first = next((ln.strip() for ln in description.splitlines() if ln.strip()), "untitled")
    return first if len(first) <= length else first[:length].rstrip() + "..."


# ---------------------------------------------------------------------------
# Open Question matching (PROJECT_PLAN.md, "Matching a new solution to an Open Question", steps 1-2)
# ---------------------------------------------------------------------------

def _still_open(conn: psycopg.Connection, post_id: int) -> dict[str, Any] | None:
    """Lock the post and return it if it is still an Open Question."""
    if not db.lock_post(conn, post_id):
        return None
    post = db.get_post(conn, post_id)
    return post if post and post["solution"] is None else None


def find_open_question(conn: psycopg.Connection, fields: dict[str, Any],
                       wanted_id: int | None = None) -> dict[str, Any] | None:
    """The Open Question this is about: the given id, else the same fingerprint, else a close text match."""
    if wanted_id is not None and (post := _still_open(conn, wanted_id)):
        return post
    if fields["fingerprint"]:
        for same in db.posts_with_fingerprint(conn, fields["fingerprint"], open_only=True):
            if post := _still_open(conn, same["id"]):
                return post
    query = matching.words(f"{fields['description']} {fields['command'] or ''}")
    # Require a shared tag, unless the post only got the fallback tag.
    tags = None if fields["tags"] == [db.FALLBACK_FORUM] else fields["tags"]
    best = matching.best_text_match(query, db.candidate_posts(conn, open_only=True),
                                    matching.ATTACH_MIN_SCORE, tags=tags)
    return _still_open(conn, best[0]["id"]) if best else None


def notify_solved(conn: psycopg.Connection, question: dict[str, Any]) -> None:
    message = (f"Your open question '{headline(question['description'])}' now has a solution "
               f"in post #{question['id']}.")
    for project in db.watchers(conn, question["id"]):
        db.add_update(conn, project, message, question["id"])


# ---------------------------------------------------------------------------
# 1. GET /v1/search
# ---------------------------------------------------------------------------

@router.get("/search")
def search(error: str = "", command: str = "", fingerprint: str = "", tags: str = "") -> list[dict[str, Any]]:
    """Matching posts, best first: exact fingerprint matches (score 1.0), then text matches (score >= 0.2).
    `tags` (comma-separated) narrows the text matches to posts sharing a tag; unknown tags are ignored."""
    fp = clean_fingerprint(fingerprint)
    query = matching.words(f"{error[:MAX_QUERY_CHARS]} {command[:LIMITS['command']]}")
    results: list[dict[str, Any]] = []
    with db.connect() as conn:
        exact = db.posts_with_fingerprint(conn, fp) if fp else []
        results += [{**db.public_post(p), "match": "fingerprint", "score": 1.0} for p in exact]
        if query:
            wanted = requested_tags(tags)
            tag_filter = [t for t in wanted if t in db.known_tags(conn)] if wanted else []
            seen = {p["id"] for p in exact}
            for post in db.candidate_posts(conn, tags=tag_filter or None):
                if post["id"] in seen:
                    continue
                score = matching.text_score(query, post["description"])
                if score >= matching.SEARCH_MIN_SCORE:
                    results.append({**db.public_post(post), "match": "text", "score": score})
    return matching.rank(results)[:MAX_SEARCH_RESULTS]


# ---------------------------------------------------------------------------
# 2. POST /v1/outcomes
# ---------------------------------------------------------------------------

@router.post("/outcomes", dependencies=[Depends(require_api_key)])
def outcomes(raw: Any = Body(default=None)) -> dict[str, str]:
    """The hook saw a suggested fix work or fail. Counts it and returns the post's status."""
    body = as_object(raw)
    post_id = clean_id(body.get("post_id"))
    if post_id is None:
        raise bad_request("`post_id` must be an integer.")
    if not isinstance(body.get("worked"), bool):
        raise bad_request("`worked` must be true or false.")
    with db.connect() as conn:
        status = db.record_outcome(
            conn, post_id, body["worked"], clean_fingerprint(body.get("fingerprint")),
            scrub(str(body.get("command") or ""))[:LIMITS["command"]] or None,
            str(body.get("session_id") or "")[:200] or None, str(body.get("source") or "")[:50] or None,
        )
    if status is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"post_status": status}


# ---------------------------------------------------------------------------
# 3. GET /v1/updates
# ---------------------------------------------------------------------------

@router.get("/updates")
def updates(project: str = "") -> list[dict[str, Any]]:
    """News for a project, each update returned only once. [] without a project id."""
    if not project.strip():
        return []
    with db.connect() as conn:
        return db.take_updates(conn, project.strip()[:200])


# ---------------------------------------------------------------------------
# 4. POST /v1/judge
# ---------------------------------------------------------------------------

@router.post("/judge", dependencies=[Depends(require_api_key)])
def judge_stuck(raw: Any = Body(default=None)) -> dict[str, Any]:
    """Ask a small model whether the agent is stuck. Any model problem -> not stuck, confidence 0."""
    body = as_object(raw)
    actions = body.get("actions")
    if not isinstance(actions, list):
        raise bad_request("`actions` must be a list.")
    replies = body.get("replies") if isinstance(body.get("replies"), list) else []
    return judge.ask(actions, replies)


# ---------------------------------------------------------------------------
# 5. POST /v1/posts
# ---------------------------------------------------------------------------

@router.post("/posts", dependencies=[Depends(require_api_key)])
def create_post(raw: Any = Body(default=None)) -> dict[str, Any]:
    """A solved problem. Fills in a matching Open Question instead of creating a new post when there is one."""
    body = as_object(raw)
    require_fields(body, ["description", "value", "solution"])
    fields = clean_post(body)
    with db.connect() as conn:
        forum, extras = place(conn, fields)
        question = find_open_question(conn, fields, clean_id(body.get("open_question_id")))
        if question and db.fill_open_question(conn, question["id"], fields["solution"],
                                              fields["fingerprint"], fields["command"]):
            notify_solved(conn, question)
            return {"post_id": question["id"], "attached_to_open_question": True, "merged_into": None,
                    **extras, "forum": question["forum"], "tags": list(question["tags"])}
        post_id = db.insert_post(conn, fields, forum["id"])
    return {"post_id": post_id, "attached_to_open_question": False, "merged_into": None, **extras}


# ---------------------------------------------------------------------------
# 6. POST /v1/open-questions
# ---------------------------------------------------------------------------

@router.post("/open-questions", dependencies=[Depends(require_api_key)])
def create_open_question(raw: Any = Body(default=None)) -> dict[str, Any]:
    """A problem nobody could solve. Returns the existing Open Question instead of a duplicate."""
    body = as_object(raw)
    require_fields(body, ["description", "value"])
    fields = clean_post({**body, "solution": None})
    with db.connect() as conn:
        forum, extras = place(conn, fields)
        existing = find_open_question(conn, fields)
        if existing:
            if fields["project"]:
                db.add_watcher(conn, existing["id"], fields["project"])
            return {"post_id": existing["id"], "duplicate_of": existing["id"], **extras,
                    "forum": existing["forum"], "tags": list(existing["tags"])}
        post_id = db.insert_post(conn, fields, forum["id"])
    return {"post_id": post_id, "duplicate_of": None, **extras}


# ---------------------------------------------------------------------------
# 7. GET /v1/posts/{id}   8. GET /v1/posts
# ---------------------------------------------------------------------------

@router.get("/posts/{post_id}")
def get_post(post_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        post = db.get_post(conn, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return db.public_post(post)


@router.get("/posts")
def list_posts(status: str = Query(default="all", alias="filter"), q: str = "",
               limit: int = 50) -> list[dict[str, Any]]:
    """Posts for the website feed, newest first. filter = all | solved | open; q searches the text."""
    if status not in FILTERS:
        raise bad_request("`filter` must be all, solved or open.")
    if not 1 <= limit <= 200:
        raise bad_request("`limit` must be between 1 and 200.")
    with db.connect() as conn:
        rows = db.list_posts(conn, FILTERS[status], q.strip()[:200] or None, limit)
    return [db.public_post(r) for r in rows]
