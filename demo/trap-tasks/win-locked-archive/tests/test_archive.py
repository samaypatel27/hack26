import csv
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from dbarchive import archive_day

SALES = [
    ("2026-10-24T09:12:00", "COF-12", 2, 3.50),
    ("2026-10-24T09:40:00", "BAG-01", 1, 2.25),
    ("2026-10-24T11:03:00", "MUF-03", 3, 2.75),
    ("2026-10-24T13:27:00", "COF-12", 1, 3.50),
    ("2026-10-24T16:55:00", "TEA-07", 2, 2.95),
]


def make_terminal_db(folder: Path) -> Path:
    """A database like the ones on the POS terminals (old firmware: no refunds table)."""
    db = folder / "pos-17.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, sold_at TEXT, sku TEXT, qty INTEGER, price REAL)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO sales (sold_at, sku, qty, price) VALUES (?, ?, ?, ?)", SALES)
    con.commit()
    con.close()
    (folder / "pos-17.log").write_text("2026-10-24 06:00:01 terminal boot fw=3.2.1\n", encoding="utf-8")
    return db


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dbarchive-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = make_terminal_db(self.tmp)
        self.archive_root = self.tmp / "archive"

    def test_database_is_moved_into_the_archive(self):
        result = archive_day(self.db, self.archive_root, date(2026, 10, 24))
        self.assertFalse(self.db.exists(), "the terminal's database should be moved, not copied")
        self.assertEqual(result.archived_db, self.archive_root / "2026-10-24" / "pos-17.db")
        con = sqlite3.connect(result.archived_db)
        try:
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key = 'archived_on'").fetchone()[0],
                             "2026-10-24")
        finally:
            con.close()

    def test_log_is_moved_with_the_database(self):
        result = archive_day(self.db, self.archive_root, date(2026, 10, 24))
        self.assertFalse((self.tmp / "pos-17.log").exists(), "the terminal's log should be moved too")
        text = result.archived_log.read_text(encoding="utf-8")
        self.assertIn("terminal boot", text)
        self.assertIn("archived 5 sales, total 26.90", text)

    def test_csv_export(self):
        result = archive_day(self.db, self.archive_root, date(2026, 10, 24))
        with open(result.csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["id", "sold_at", "sku", "qty", "price"])
        self.assertEqual(len(rows), 6)

    def test_summary(self):
        result = archive_day(self.db, self.archive_root, date(2026, 10, 24))
        self.assertEqual((result.sale_count, result.total), (5, "26.90"))
        self.assertEqual(result.preview, ["COF-12", "BAG-01", "MUF-03"])


if __name__ == "__main__":
    unittest.main()
