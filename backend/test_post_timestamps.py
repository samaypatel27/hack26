"""Check resolution timestamps in a temporary table, without modifying forum posts."""
import os
import unittest
from pathlib import Path

import psycopg
from dotenv import load_dotenv


class PostTimestampTests(unittest.TestCase):
    def test_resolution_preserves_posting_time_and_ignores_later_edits(self):
        load_dotenv(Path(__file__).with_name(".env"))
        migration = (Path(__file__).parent / "migrations/20261025_post_timestamps.sql").read_text()
        function = migration.split("CREATE OR REPLACE FUNCTION", 1)[1].split("CREATE TRIGGER", 1)[0]
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15) as conn:
            conn.execute("CREATE TEMP TABLE timestamp_posts (id int, solution text, general_solution text, status text, created_at timestamptz DEFAULT now(), solved_at timestamptz)")
            conn.execute("CREATE OR REPLACE FUNCTION" + function.replace("public.track_post_resolution", "pg_temp.track_post_resolution"))
            conn.execute("CREATE TRIGGER track BEFORE INSERT OR UPDATE ON timestamp_posts FOR EACH ROW EXECUTE FUNCTION pg_temp.track_post_resolution()")
            conn.execute("INSERT INTO timestamp_posts(id,status) VALUES (1,'solved')")
            created, status, solved = conn.execute("SELECT created_at,status,solved_at FROM timestamp_posts").fetchone()
            self.assertEqual(status, "open")
            self.assertIsNone(solved)
            conn.execute("UPDATE timestamp_posts SET general_solution='A solution'")
            posted, status, solved = conn.execute("SELECT created_at,status,solved_at FROM timestamp_posts").fetchone()
            self.assertEqual(posted, created)
            self.assertEqual(status, "solved")
            self.assertIsNotNone(solved)
            conn.execute("UPDATE timestamp_posts SET general_solution='An edited solution'")
            self.assertEqual(conn.execute("SELECT solved_at FROM timestamp_posts").fetchone()[0], solved)
            conn.execute("UPDATE timestamp_posts SET general_solution=NULL")
            self.assertEqual(conn.execute("SELECT status,solved_at FROM timestamp_posts").fetchone(), ("open", None))
            conn.rollback()
