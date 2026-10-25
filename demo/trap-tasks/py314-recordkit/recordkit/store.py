"""Persist records in SQLite: one table per model, one column per field (lists and nested records as JSON)."""

import json
import sqlite3
from decimal import Decimal


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row

    def save(self, record, **extra):
        """Insert a record. `extra` adds computed columns, e.g. save(order, total=order.total())."""
        table = type(record).__name__.lower()
        row = {name: _column(value) for name, value in record.to_dict().items()}
        row.update({name: _column(value) for name, value in extra.items()})
        columns = ", ".join(row)
        self.db.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns})')
        placeholders = ", ".join(f":{name}" for name in row)
        self.db.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(row.values()))
        self.db.commit()

    def get(self, table, **where):
        """The first row of `table` matching all of `where`, as a dict (or None)."""
        condition = " AND ".join(f"{name} = :{name}" for name in where)
        found = self.db.execute(f'SELECT * FROM "{table}" WHERE {condition}', where).fetchone()
        return dict(found) if found else None


def _column(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, Decimal):
        return str(value)
    return value
