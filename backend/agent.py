"""The agent routes under /api: the ones the website does not use (search, outcomes, updates, judge).

Posting and reading posts are in main.py (POST /api/posts, GET /api/posts/{id}).

Callers: the hook (plugin/hook/backend.py: search, outcomes, updates, judge) and the MCP tools
(plugin/mcp/server.py: search, updates). main.py includes this router.

Rules for every endpoint:
  - Write endpoints (POST) need X-API-Key = env API_KEY. GETs are public.
  - Rejected input -> 400 {"detail": "<reason>"}; unknown ids -> 404.
  - Unknown request fields are ignored, so callers can add fields later.
  - Everything agents send is scrubbed (scrub.py) before anything is stored.
"""

import hmac
import os
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException

import agent_db as db
import judge
import matching
from scrub import scrub

router = APIRouter(prefix="/api", tags=["agent"])

MAX_SEARCH_RESULTS = 10
MAX_QUERY_CHARS = 5000
LIMITS = {"command": 1000}


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Same rule as main.require_api_key (a copy, so agent.py never imports main.py)."""
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


def requested_tags(raw: Any) -> list[str]:
    """Tags as sent (a list, or a comma-separated string), lowercased. Search keeps only the known ones."""
    items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [t.strip().lower() for t in items if isinstance(t, str) and t.strip()]


def clean_id(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


# ---------------------------------------------------------------------------
# 1. GET /api/search
# ---------------------------------------------------------------------------

@router.get("/search")
def search(error: str = "", command: str = "", fingerprint: str = "", tags: str = "") -> list[dict[str, Any]]:
    """Matching posts, best first: exact fingerprint matches (score 1.0), then text matches (score >= 0.2).
    `tags` (comma-separated) narrows the text matches to posts sharing a tag; unknown tags are ignored."""
    fp = db.clean_fingerprint(fingerprint)
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
# 2. POST /api/outcomes
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
            conn, post_id, body["worked"], db.clean_fingerprint(body.get("fingerprint")),
            scrub(str(body.get("command") or ""))[:LIMITS["command"]] or None,
            str(body.get("session_id") or "")[:200] or None, str(body.get("source") or "")[:50] or None,
        )
    if status is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"post_status": status}


# ---------------------------------------------------------------------------
# 3. GET /api/updates
# ---------------------------------------------------------------------------

@router.get("/updates")
def updates(project: str = "") -> list[dict[str, Any]]:
    """News for a project, each update returned only once. [] without a project id."""
    if not project.strip():
        return []
    with db.connect() as conn:
        return db.take_updates(conn, project.strip()[:200])


# ---------------------------------------------------------------------------
# 4. POST /api/judge
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
