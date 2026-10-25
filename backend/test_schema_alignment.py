"""Integration checks: run with backend/.venv/Scripts/python.exe -m unittest backend.test_schema_alignment.

Reads the configured database; seed writes are isolated in temporary tables.
On the real (shared) database run only the read-only checks:
    backend/.venv/Scripts/python.exe -m unittest backend.test_schema_alignment.SchemaAlignmentTests.test_feed_detail_and_tag_search backend.test_schema_alignment.SchemaAlignmentTests.test_live_columns_match_the_test_stub
"""
import unittest
import os
import psycopg
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
from fastapi.testclient import TestClient

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))   # main.py imports its neighbours as top-level modules
import main  # noqa: E402
import seed  # noqa: E402

load_dotenv(Path(__file__).with_name(".env"))

# The columns the code expects, as in tests/breakpoint_schema_stub.sql (the copy of the real tables).
STUB_TABLES = {
    "forums": ("id", "slug", "name", "description"),
    "tags": ("id", "slug"),
    "agents": ("id", "model_name", "created_at", "email"),
    "posts": ("id", "value", "description", "solution", "post_votes", "status", "created_at", "author",
              "solution_votes", "forum_id", "problem", "relevancy", "general_description", "general_solution",
              "fingerprint", "solved_at", "agent_id"),
    "post_tags": ("post_id", "tag_id"),
    "forum_tags": ("forum_id", "tag_id"),
    "post_visits": ("post_id", "agent_id", "created_at"),
    "post_useful": ("post_id", "agent_id", "created_at"),
    "replies": ("id", "post_id", "agent_id", "body", "request_id", "created_at"),
    "reply_upvotes": ("reply_id", "agent_id", "created_at"),
    "post_cards": ("id", "description", "value", "solution", "post_votes", "solution_votes", "author", "status",
                   "created_at", "forum", "forum_name", "tags", "problem", "relevancy", "general_description",
                   "general_solution", "visited_count", "useful_count", "reply_count", "solved_at", "agent_id"),
    "forum_overview": ("slug", "name", "description", "tags", "post_count"),
}


class SchemaAlignmentTests(unittest.TestCase):
    def test_feed_detail_and_tag_search(self):
        client = TestClient(main.app)
        posts = client.get("/api/posts?limit=200").json()
        self.assertTrue(posts, "Expected sample posts in the configured database")
        for post in posts:
            self.assertIsInstance(post["tags"], list)
            self.assertIsInstance(post["upvotes"], int)
        tagged = next(p for p in posts if p["tags"])
        detail = client.get(f"/api/posts/{tagged['id']}").json()
        self.assertEqual(detail["tags"], tagged["tags"])
        self.assertEqual(detail["upvotes"], tagged["upvotes"])
        matches = client.get("/api/posts", params={"q": tagged["tags"][0], "limit": 200}).json()
        self.assertIn(tagged["id"], [p["id"] for p in matches])
        for status in ("open", "solved"):
            self.assertTrue(all(p["status"] == status for p in client.get("/api/posts", params={"filter": status, "limit": 200}).json()))

    def test_live_columns_match_the_test_stub(self):
        """READ-ONLY: every table/view column in tests/breakpoint_schema_stub.sql exists in the live database
        with the same name, and the live database has nothing the stub lacks (information_schema only)."""
        with psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None) as conn:
            conn.read_only = True
            live = {(t, c) for t, c in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
                " AND table_name = ANY(%s)", (list(STUB_TABLES),)).fetchall()}
        self.assertEqual(live, {(t, c) for t, cols in STUB_TABLES.items() for c in cols})

    def test_seed_inserts_normalized_tags_and_is_repeatable(self):
        # NOTE: this one creates TEMP tables on the configured database (session-only, but still writes).
        # Run it only against a throwaway database.
        # Temporary tables shadow the real ones for this connection only.
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            for table in ("forums", "posts", "tags", "post_tags"):
                conn.execute(f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL)")
            conn.execute("INSERT INTO forums (slug, name) VALUES ('general', 'General')")
            with patch.object(seed.psycopg, "connect", return_value=nullcontext(conn)):
                seed.main()
                seed.main()
            self.assertEqual(conn.execute("SELECT count(*) FROM posts").fetchone()[0], len(seed.SAMPLES))
            self.assertEqual(conn.execute("SELECT count(*) FROM post_tags").fetchone()[0], sum(len(p[4]) for p in seed.SAMPLES))
            self.assertEqual(conn.execute("SELECT count(*) FROM posts p JOIN forums f ON f.id=p.forum_id").fetchone()[0], len(seed.SAMPLES))
            self.assertEqual(conn.execute("SELECT count(*) FROM posts WHERE problem IS NOT NULL AND general_description IS NOT NULL").fetchone()[0], len(seed.SAMPLES))


if __name__ == "__main__":
    unittest.main()
