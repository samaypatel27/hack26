"""Declarative records.

    class Point(Model):
        x: int
        y: int = 0

    Point(x=1)            -> Point(x=1, y=0)
    Point(x=1).to_dict()  -> {"x": 1, "y": 0}
"""

import copy
import functools
from decimal import Decimal

from .money import quantize

MISSING = object()


class Field:
    __slots__ = ("name", "type", "default")

    def __init__(self, name, type_, default=MISSING):
        self.name, self.type, self.default = name, type_, default

    def __repr__(self):
        return f"Field({self.name!r}, {self.type!r})"


class ModelMeta(type):
    """Collects the annotated fields of each Model subclass into cls.__fields__ (in order, base classes first)."""

    def __new__(mcls, name, bases, ns, **kwargs):
        fields = {}
        for base in reversed(bases):
            fields.update(getattr(base, "__fields__", {}))
        for fname, ftype in ns.get("__annotations__", {}).items():
            if fname.startswith("_"):
                continue
            fields[fname] = Field(fname, ftype, ns.pop(fname, MISSING))
        ns["__fields__"] = fields
        return super().__new__(mcls, name, bases, ns, **kwargs)


class Model(metaclass=ModelMeta):
    # Decimal values are stored as given and rounded to cents when exported.
    _cents = functools.partial(quantize, exp=Decimal("0.01"))

    def __init__(self, **values):
        cls = type(self)
        for key in values:
            if key not in cls.__fields__:
                raise TypeError(f"{cls.__name__} has no field {key!r}")
        for f in cls.__fields__.values():
            if f.name in values:
                value = values[f.name]
            elif f.default is not MISSING:
                value = copy.copy(f.default)
            else:
                raise TypeError(f"{cls.__name__} is missing required field {f.name!r}")
            if isinstance(f.type, type) and value is not None and not isinstance(value, f.type):
                raise TypeError(f"{cls.__name__}.{f.name} must be {f.type.__name__}, got {type(value).__name__}")
            setattr(self, f.name, value)

    def __repr__(self):
        inner = ", ".join(f"{name}={getattr(self, name)!r}" for name in type(self).__fields__)
        return f"{type(self).__name__}({inner})"

    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def to_dict(self):
        return {name: self._export(getattr(self, name)) for name in type(self).__fields__}

    def _export(self, value):
        if isinstance(value, Model):
            return value.to_dict()
        if isinstance(value, list):
            return [self._export(v) for v in value]
        if isinstance(value, Decimal):
            return str(self._cents(value))
        return value
