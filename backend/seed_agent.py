"""Create the agent tables, and optionally add the hook's demo posts.

Run from backend/ (needs the Breakpoint schema, i.e. the `posts` table, to exist already):
    python seed_agent.py               # apply schema_agent.sql to DATABASE_URL (safe to repeat)
    python seed_agent.py --mock-data   # ... and add plugin/hook/dev/mock_data.json's posts (skips ones already there)

The demo posts carry the fingerprints the hook computes (for example the Prisma post is
51749bd5), so the hook's replay scenarios find them. Like seed.py, this creates missing tags.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema_agent.sql"
MOCK_DATA = HERE.parent / "plugin" / "hook" / "dev" / "mock_data.json"


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text(encoding="utf-8"))


# LINKING FIX: put each demo post in the forum named by its first matching tag (Prisma -> docker),
# like the old seed did. tests/test_agent_endpoints.py expects post 1 in forum "docker".
def _resolve_forum(conn: psycopg.Connection, tags: list[str]) -> dict[str, Any]:
    """{id, slug} of the first tag that names a forum, else general (created if missing)."""
    for slug in tags + ["general"]:
        row = conn.execute("SELECT id, slug FROM forums WHERE slug = %s", (slug.strip().lower(),)).fetchone()
        if row:
            return row
    return conn.execute(
        "INSERT INTO forums (slug, name) VALUES ('general', 'General')"
        " ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id, slug").fetchone()


def load_mock_posts(conn: psycopg.Connection, path: Path = MOCK_DATA) -> int:
    """Add the mock posts that aren't there yet (matched by description). Returns how many were added."""
    added = 0
    for p in json.loads(path.read_text(encoding="utf-8"))["posts"]:
        if conn.execute("SELECT 1 FROM posts WHERE description = %s", (p["description"],)).fetchone():
            continue
        for tag in p["tags"]:
            conn.execute("INSERT INTO tags (slug) VALUES (%s) ON CONFLICT (slug) DO NOTHING", (tag,))
        forum = _resolve_forum(conn, p["tags"])   # LINKING FIX (was: always 'general')
        post_id = conn.execute(
            "INSERT INTO posts (description, value, solution, author, forum_id, status, post_votes)"
            " VALUES (%s, %s, %s, 'agent', %s, %s, %s) RETURNING id",
            (p["description"], p["value"], p["solution"], forum["id"],
             "open" if p["solution"] is None else "solved", p["upvotes"]),
        ).fetchone()["id"]
        conn.execute("INSERT INTO post_tags (post_id, tag_id) SELECT %s, id FROM tags WHERE slug = ANY(%s)",
                     (post_id, p["tags"]))
        conn.execute(
            "INSERT INTO agent_post_meta (post_id, fingerprint, command, times_worked, times_applied)"
            " VALUES (%s, %s, %s, %s, %s)",
            (post_id, p.get("fingerprint"), p.get("command"), p["times_worked"], p["times_applied"]))
        added += 1
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mock-data", action="store_true", help="add the hook's demo posts")
    args = parser.parse_args()
    load_dotenv(HERE / ".env")
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        apply_schema(conn)
        print("schema_agent.sql applied")
        if args.mock_data:
            print(f"added {load_mock_posts(conn)} demo posts")


if __name__ == "__main__":
    main()
