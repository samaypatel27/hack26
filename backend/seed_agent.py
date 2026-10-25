"""Add the hook's demo posts (plugin/hook/dev/mock_data.json) to the Breakpoint tables.

Run from backend/ (the tables must already exist; this script never creates or changes tables):
    python seed_agent.py --mock-data   # add the demo posts that are not there yet (matched by description)

The demo posts carry the fingerprints the hook computes (for example the Prisma post is 51749bd5), stored
in posts.fingerprint, so the hook's replay scenarios find them. Like seed.py, this creates missing tags.
The tests (tests/conftest.py) use load_mock_posts on a throwaway database.

Careful: with the real DATABASE_URL this writes demo posts into the shared database.
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
MOCK_DATA = HERE.parent / "plugin" / "hook" / "dev" / "mock_data.json"


# Put each demo post in the forum named by its first matching tag (Prisma -> docker), like the old seed
# did. tests/test_agent_endpoints.py expects post 1 in forum "docker".
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
        forum = _resolve_forum(conn, p["tags"])
        post_id = conn.execute(
            "INSERT INTO posts (description, value, solution, author, forum_id, status, post_votes, fingerprint)"
            " VALUES (%s, %s, %s, 'agent', %s, %s, %s, %s) RETURNING id",
            (p["description"], p["value"], p["solution"], forum["id"],
             "open" if p["solution"] is None else "solved", p["upvotes"], p.get("fingerprint")),
        ).fetchone()["id"]
        conn.execute("INSERT INTO post_tags (post_id, tag_id) SELECT %s, id FROM tags WHERE slug = ANY(%s)",
                     (post_id, p["tags"]))
        added += 1
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mock-data", action="store_true", help="add the hook's demo posts")
    args = parser.parse_args()
    if not args.mock_data:
        parser.error("nothing to do: pass --mock-data to add the demo posts (the tables already exist)")
    load_dotenv(HERE / ".env")
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        print(f"added {load_mock_posts(conn)} demo posts")


if __name__ == "__main__":
    main()
