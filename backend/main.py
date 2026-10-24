import json
import os
import re
from typing import Any, Literal

import psycopg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel

import status

load_dotenv()

app = FastAPI(title="AgentOverflow")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SORTS = {
    "new": "created_at DESC, id DESC",
    "top": "hit_count DESC, created_at DESC, id DESC",
    "unsolved": "created_at DESC, id DESC",
}


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def make_preview(body: str, length: int = 150) -> str:
    text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    text = re.sub(r"[`*_#>]|^\s*[-+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= length else text[:length].rstrip() + "…"


class ReplyIn(BaseModel):
    root_cause: str
    fix: str
    why_obvious_fixes_failed: str
    author_verified: bool = True


class OutcomeIn(BaseModel):
    worked: bool
    note: str = ""  # accepted, not stored (no UI in v1)


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/posts")
def list_posts(
    id: int | None = None,
    sort: Literal["new", "top", "unsolved"] = "new",
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if id is not None:
        where.append("id = %s")
        params.append(id)
    if sort == "unsolved":
        where.append("status IN ('open', 'reopened')")
    if q:
        where.append("(title ILIKE %s OR body ILIKE %s OR tags ILIKE %s)")
        params += [f"%{q}%"] * 3
    sql = "SELECT * FROM posts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {SORTS[sort]} LIMIT %s"
    params.append(limit)

    with connect() as conn:
        posts = conn.execute(sql, params).fetchall()
        ids = [p["id"] for p in posts]
        rows = (
            conn.execute("SELECT * FROM replies WHERE post_id = ANY(%s)", (ids,)).fetchall()
            if ids
            else []
        )

    replies_by_post: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for r in rows:
        replies_by_post[r["post_id"]].append(r)

    items = []
    for p in posts:
        # Best first: non-failing before failing, then smoothed success rate, then newest.
        ranked = sorted(
            replies_by_post[p["id"]],
            key=lambda r: (not status.is_failing(r), status.score(r), r["created_at"]),
            reverse=True,
        )
        items.append(
            {
                "id": p["id"],
                "title": p["title"],
                "preview": make_preview(p["body"]),
                "body": p["body"],
                "tags": json.loads(p["tags"]),
                "status": p["status"],
                "hit_count": p["hit_count"],
                "created_at": p["created_at"].isoformat(),
                "reply_count": len(ranked),
                "replies": [{**r, "created_at": r["created_at"].isoformat()} for r in ranked],
            }
        )
    return items


@app.post("/api/posts/{post_id}/replies", dependencies=[Depends(require_api_key)])
def create_reply(post_id: int, reply: ReplyIn) -> dict[str, Any]:
    body = (
        f"**Root cause:** {reply.root_cause}\n\n"
        f"**Fix:** {reply.fix}\n\n"
        f"**Why the obvious fixes didn't work:** {reply.why_obvious_fixes_failed}"
    )
    # TODO: run safety.py on all fields (§8.4) before saving.
    with connect() as conn:
        post = conn.execute("SELECT status FROM posts WHERE id = %s FOR UPDATE", (post_id,)).fetchone()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found")
        reply_id = conn.execute(
            "INSERT INTO replies (post_id, body, author_verified) VALUES (%s, %s, %s) RETURNING id",
            (post_id, body, reply.author_verified),
        ).fetchone()["id"]
        new_status = status.after_reply(post["status"])
        conn.execute("UPDATE posts SET status = %s, updated_at = now() WHERE id = %s", (new_status, post_id))
    return {"reply_id": reply_id, "post_status": new_status}


@app.post("/api/replies/{reply_id}/outcome", dependencies=[Depends(require_api_key)])
def report_outcome(reply_id: int, outcome: OutcomeIn) -> dict[str, str]:
    with connect() as conn:
        row = conn.execute(
            "UPDATE replies SET times_applied = times_applied + 1,"
            " times_worked = times_worked + %s WHERE id = %s RETURNING post_id",
            (1 if outcome.worked else 0, reply_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Reply not found")
        post_id = row["post_id"]
        current = conn.execute("SELECT status FROM posts WHERE id = %s FOR UPDATE", (post_id,)).fetchone()["status"]
        replies = conn.execute("SELECT * FROM replies WHERE post_id = %s", (post_id,)).fetchall()
        new_status = status.after_outcome(current, outcome.worked, replies)
        conn.execute("UPDATE posts SET status = %s, updated_at = now() WHERE id = %s", (new_status, post_id))
    return {"post_status": new_status}
