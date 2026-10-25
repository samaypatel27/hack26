"""Export sales rows to CSV."""

import csv
import sqlite3
from contextlib import closing
from pathlib import Path

COLUMNS = ["id", "sold_at", "sku", "qty", "price"]


def iter_sales(db_path):
    """Yield the sales rows one at a time, oldest first (a busy terminal has a lot of them)."""
    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        yield from con.execute(f"SELECT {', '.join(COLUMNS)} FROM sales ORDER BY id")


def export_csv(db_path, csv_path) -> Path:
    csv_path = Path(csv_path)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for row in iter_sales(db_path):
            writer.writerow([row[c] for c in COLUMNS])
    return csv_path
