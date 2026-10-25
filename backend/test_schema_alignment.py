"""Integration checks: run with backend/.venv/Scripts/python.exe -m unittest backend.test_schema_alignment.

Reads the configured database; seed writes are isolated in temporary tables.
"""
import unittest
import os
import psycopg
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

from backend import main, seed

load_dotenv(Path(__file__).with_name(".env"))


class SchemaAlignmentTests(unittest.TestCase):
    def test_feed_detail_and_tag_search(self):
        posts = main.list_posts(limit=200)
        self.assertTrue(posts, "Expected sample posts in the configured database")
        for post in posts:
            self.assertIsInstance(post["tags"], list)
            self.assertIsInstance(post["upvotes"], int)
        tagged = next(p for p in posts if p["tags"])
        detail = main.get_post(tagged["id"])
        self.assertEqual(detail["tags"], tagged["tags"])
        self.assertEqual(detail["upvotes"], tagged["upvotes"])
        matches = main.list_posts(q=tagged["tags"][0], limit=200)
        self.assertIn(tagged["id"], [p["id"] for p in matches])
        for status in ("open", "solved"):
            self.assertTrue(all(p["status"] == status for p in main.list_posts(filter=status, limit=200)))

    def test_seed_inserts_normalized_tags_and_is_repeatable(self):
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


if __name__ == "__main__":
    unittest.main()
