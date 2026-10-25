"""Persist records in SQLite: one table per model, one column per field.

Each field is stored by its declared type: str, int, float and bool as they are, Decimal as exact text, and
everything else (lists, dicts, nested records) as JSON. Computed columns passed to save() use their value's type.
"""

import json
import sqlite3
import types
import typing
from decimal import Decimal

from .model import Model

PLAIN = (str, int, float, bool)


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row

    def save(self, record, **extra):
        """Insert a record. `extra` adds computed columns, e.g. save(order, total=order.total())."""
        table = type(record).__name__.lower()
        declared = _field_types(record)
        row = {name: _column(value, declared.get(name)) for name, value in record.to_dict().items()}
        row.update({name: _column(value, type(value)) for name, value in extra.items()})
        columns = ", ".join(row)
        self.db.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns})')
        placeholders = ", ".join(f":{name}" for name in row)
        self.db.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(row.values()))
        self.db.commit()
        record._store = self

    def get(self, table, **where):
        """The first row of `table` matching all of `where`, as a dict (or None)."""
        condition = " AND ".join(f"{name} = :{name}" for name in where)
        params = {name: _column(value, type(value)) for name, value in where.items()}
        found = self.db.execute(f'SELECT * FROM "{table}" WHERE {condition}', params).fetchone()
        return dict(found) if found else None


def _field_types(record):
    """{field name: declared type} for a record. Optional[X] and X | None count as X."""
    try:
        hints = typing.get_type_hints(record)
    except (TypeError, NameError):   # a plain object, not a Model: no declared types, so every value goes in as JSON
        return {}
    return {name: _without_none(tp) for name, tp in hints.items()}


def _without_none(tp):
    args = [a for a in typing.get_args(tp) if a is not type(None)]
    union = typing.get_origin(tp) in (typing.Union, types.UnionType)
    return args[0] if union and len(args) == 1 else tp


def _column(value, tp):
    """The value to store for a field of type `tp`."""
    if value is None or tp in PLAIN:
        return value
    if tp is Decimal:
        return str(value)
    if isinstance(value, Model):
        value = value.to_dict()
    return json.dumps(value)
