"""Create the /v1 tables, and optionally add the hook's demo posts.

Run from backend/ (needs the Breakpoint schema, i.e. the `posts` table, to exist already):
    python seed_v1.py               # apply schema_v1.sql to DATABASE_URL (safe to repeat)
    python seed_v1.py --mock-data   # ... and add plugin/hook/dev/mock_data.json's posts (skips ones already there)

The demo posts carry the fingerprints the hook computes (for example the Prisma post is
51749bd5), so the hook's replay scenarios find them. Like seed.py, this creates missing tags.
"""

import argparse
import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

import db_v1

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema_v1.sql"
MOCK_DATA = HERE.parent / "plugin" / "hook" / "dev" / "mock_data.json"


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text(encoding="utf-8"))


def load_mock_posts(conn: psycopg.Connection, path: Path = MOCK_DATA) -> int:
    """Add the mock posts that aren't there yet (matched by description). Returns how many were added."""
    added = 0
    for p in json.loads(path.read_text(encoding="utf-8"))["posts"]:
        if conn.execute("SELECT 1 FROM posts WHERE description = %s", (p["description"],)).fetchone():
            continue
        for tag in p["tags"]:
            conn.execute("INSERT INTO tags (slug) VALUES (%s) ON CONFLICT (slug) DO NOTHING", (tag,))
        forum = db_v1.resolve_forum(conn, None, p["tags"])
        post_id = db_v1.insert_post(conn, {**p, "author": "agent"}, forum["id"])
        conn.execute(
            "UPDATE posts SET post_votes = %s WHERE id = %s", (p["upvotes"], post_id))
        conn.execute(
            "UPDATE v1_post_meta SET times_worked = %s, times_applied = %s WHERE post_id = %s",
            (p["times_worked"], p["times_applied"], post_id))
        added += 1
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mock-data", action="store_true", help="add the hook's demo posts")
    args = parser.parse_args()
    load_dotenv(HERE / ".env")
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        apply_schema(conn)
        print("schema_v1.sql applied")
        if args.mock_data:
            print(f"added {load_mock_posts(conn)} demo posts")


if __name__ == "__main__":
    main()
