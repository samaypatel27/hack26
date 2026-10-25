import os
from pathlib import Path
from typing import Any, Literal

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row

load_dotenv(Path(__file__).with_name(".env"))

# Aggregate per post so posts without tags remain visible and pagination is stable.
POST_SELECT = """
SELECT p.*, ARRAY(
    SELECT t.slug FROM post_tags pt
    JOIN tags t ON t.id = pt.tag_id
    WHERE pt.post_id = p.id ORDER BY t.slug
) AS tags
FROM posts p
"""

app = FastAPI(title="AgentOverflow")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def headline(description: str, length: int = 120) -> str:
    first = description.strip().splitlines()[0] if description.strip() else ""
    return first if len(first) <= length else first[:length].rstrip() + "…"


def to_card(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p["id"],
        "headline": headline(p["description"]),
        "value": p["value"],
        "tags": p["tags"],
        "upvotes": p["post_votes"],
        "status": p["status"],
        "is_open": p["status"] == "open",
        "created_at": p["created_at"].isoformat(),
    }


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/posts")
def list_posts(
    filter: Literal["all", "solved", "open"] = "all",
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if filter != "all":
        where.append("p.status = %s")
        params.append(filter)
    if q:
        where.append("""(p.description ILIKE %s OR p.value ILIKE %s OR p.solution ILIKE %s
            OR EXISTS (SELECT 1 FROM post_tags pt JOIN tags t ON t.id = pt.tag_id
                       WHERE pt.post_id = p.id AND t.slug ILIKE %s))""")
        params += [f"%{q}%"] * 4
    sql = POST_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.created_at DESC, p.id DESC LIMIT %s"
    params.append(limit)

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [to_card(p) for p in rows]


@app.get("/api/posts/{post_id}")
def get_post(post_id: int) -> dict[str, Any]:
    with connect() as conn:
        p = conn.execute(POST_SELECT + " WHERE p.id = %s", (post_id,)).fetchone()
    if p is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return {**to_card(p), "description": p["description"], "solution": p["solution"]}
