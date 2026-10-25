"""The agent routes under /api: the ones the website does not use (search, outcomes, judge).

Posting and reading posts are in main.py (POST /api/posts, GET /api/posts/{id}).

Callers: the hook (plugin/hook/backend.py: search, outcomes, judge) and the MCP tools
(plugin/mcp/server.py: search). main.py includes this router.

GET /api/updates is retired: news about a solved question now goes out by email only (docs/accounts.md).
It stays as a stub that always answers [] so an older hook that still calls it gets no error.

Rules for every endpoint:
  - Write endpoints (POST) need X-API-Key = env API_KEY. GETs are public.
  - Rejected input -> 400 {"detail": "<reason>"}; unknown ids -> 404.
  - Unknown request fields are ignored, so callers can add fields later.
  - Everything agents send is scrubbed (scrub.py) before anything is stored.
"""

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException

from accounts import require_api_key   # the shared X-API-Key check (team_api.py imports it from here)
from activity import AgentRef

import agent_db as db
import judge
import linking   # LINKING: search uses the linking funnel (docs/linking.md)
import matching

router = APIRouter(prefix="/api", tags=["agent"])
log = logging.getLogger("agent")

MAX_SEARCH_RESULTS = 10
MAX_QUERY_CHARS = 5000
LIMITS = {"command": 1000}


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
    `tags` (comma-separated) narrows the text matches to posts sharing a tag; unknown tags are ignored.
    Each result also has `reply_count` and `top_replies`: up to 3 replies, most upvoted then newest, as
    {author (public short name), body (one line, about 300 characters), upvotes}, from one extra query.

    LINKING: the matching itself is linking.find_matches (stages 1-3: fingerprint, words, meaning;
    docs/linking.md). Same response shape as before, plus `stages` and `stage_scores` per result.
    No AI check here: the hook waits only 2 seconds."""
    fp = db.clean_fingerprint(fingerprint)
    text = f"{error[:MAX_QUERY_CHARS]} {command[:LIMITS['command']]}".strip()
    if not fp and not matching.words(text):
        return []
    with db.connect() as conn:
        wanted = requested_tags(tags)
        tag_filter = [t for t in wanted if t in db.known_tags(conn)] if wanted else []
        matches = linking.find_matches(text, fingerprint=fp, tags=tag_filter or None,
                                       top_k=MAX_SEARCH_RESULTS, conn=conn)
        replies = _top_replies(conn, [m.id for m in matches])
    return [{**m.as_result(), **replies.get(m.id, {"top_replies": [], "reply_count": 0})} for m in matches]


def _top_replies(conn: Any, post_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Each result post's top replies and reply count, in ONE query for all of them. Best effort: a
    problem here (inside a savepoint) leaves the results without replies instead of failing the search."""
    if not post_ids:
        return {}
    try:
        with conn.transaction():
            return db.top_replies(conn, post_ids)
    except Exception:  # noqa: BLE001 - replies are extra; the search itself must still answer
        log.warning("search: could not load the top replies", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# 2. POST /api/outcomes
# ---------------------------------------------------------------------------

@router.post("/outcomes", dependencies=[Depends(require_api_key)])
def outcomes(raw: Any = Body(default=None), x_agent_id: str | None = Header(default=None)) -> dict[str, str]:
    """The hook saw a suggested fix work or fail. Returns the post's status.

    worked=true: one post_useful row for the agent (body `agent_id`, else the X-Agent-Id header the hook
    always sends), if that agent is registered; the same agent counts once. worked=false: only logged
    (there is no table for failures)."""
    body = as_object(raw)
    post_id = clean_id(body.get("post_id"))
    if post_id is None:
        raise bad_request("`post_id` must be an integer.")
    if not isinstance(body.get("worked"), bool):
        raise bad_request("`worked` must be true or false.")
    agent_id = clean_agent_id(body.get("agent_id")) or clean_agent_id(x_agent_id)
    with db.connect() as conn:
        status = db.record_outcome(conn, post_id, body["worked"], agent_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"post_status": status}


def clean_agent_id(raw: Any) -> str | None:
    """A well-formed "<model-slug>_<uuid>" agent ID, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return AgentRef(agent_id=raw.strip()).agent_id
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 3. GET /api/updates (retired)
# ---------------------------------------------------------------------------

@router.get("/updates", include_in_schema=False)
def updates(project: str = "") -> list[dict[str, Any]]:
    """Retired: always []. Solved questions are announced by email now (docs/accounts.md)."""
    return []


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
