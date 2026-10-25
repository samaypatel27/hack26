"""Daily totals for the summary e-mail."""

import functools
import sqlite3
from decimal import Decimal

# Problems worth mentioning in the nightly log (for example an old terminal without a refunds table).
warnings = []


@functools.lru_cache(maxsize=None)
def _connect(path: str) -> sqlite3.Connection:
    """Opening a database is slow on the terminals' SD cards, so keep one connection per file."""
    return sqlite3.connect(path)


def summary(db_path) -> dict:
    con = _connect(str(db_path))
    count, total = con.execute("SELECT COUNT(*), COALESCE(SUM(qty * price), 0) FROM sales").fetchone()
    try:
        refunds = con.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
    except sqlite3.OperationalError as exc:      # older terminals have no refunds table
        warnings.append(exc)
        refunds = 0
    return {"count": count, "total": str(Decimal(str(total)).quantize(Decimal("0.01"))), "refunds": refunds}
