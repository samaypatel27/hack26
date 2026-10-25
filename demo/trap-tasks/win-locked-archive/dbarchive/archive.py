"""Nightly archive: export the day's sales, write a summary, then move the terminal's database and log into the
archive."""

import itertools
import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .export import export_csv, iter_sales
from .joblog import job_logger
from .stats import summary


@dataclass
class ArchiveResult:
    archived_db: Path
    archived_log: Path
    csv_path: Path
    sale_count: int
    total: str
    preview: list


def archive_day(db_path, archive_root, day: date) -> ArchiveResult:
    db_path = Path(db_path)
    log_path = db_path.with_suffix(".log")
    dest_dir = Path(archive_root) / day.isoformat()
    dest_dir.mkdir(parents=True, exist_ok=True)
    log = job_logger(log_path)
    log.info("archive of %s started", day)

    # Stamp the database so a restored copy shows when it was archived.
    with sqlite3.connect(db_path) as con:
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('archived_on', ?)", (day.isoformat(),))

    # First rows for the e-mail report.
    sales = iter_sales(db_path)
    preview = [row["sku"] for row in itertools.islice(sales, 3)]

    csv_path = export_csv(db_path, dest_dir / "sales.csv")
    stats = summary(db_path)
    log.info("archived %d sales, total %s", stats["count"], stats["total"])

    archived_db = dest_dir / db_path.name
    archived_log = dest_dir / log_path.name
    os.replace(db_path, archived_db)
    os.replace(log_path, archived_log)
    return ArchiveResult(archived_db, archived_log, csv_path, stats["count"], stats["total"], preview)
