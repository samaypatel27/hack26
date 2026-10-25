# Solution (for the demo team only; setup_trap.py never copies this file)

## What the agent sees

`python -m unittest`: the Customer tests pass, the Order and Store tests fail (exit code 1):

```
TypeError: LineItem has no field 'sku'
...
AssertionError: Lists differ: [] != ['id', 'lines', 'tax_rule', 'coupon']
FAILED (failures=1, errors=5)
```

`shop/order.py` clearly declares `sku: str`, so the error looks impossible.

## Why it is a trap

The code was written for Python 3.10-3.13 and is correct there. Python 3.14 changed **four** things it relies on.
They are on one code path (build the record -> export it -> price it -> save it), so they show up one at a time,
each with a different error. Even an agent that fixes every layer on the first try fails 4 times with 4
different errors, so the hook's "whack-a-mole" signal (4+ failures, 3+ different errors) always fires.

| Layer | Error after the previous layers are fixed | Fingerprint |
| --- | --- | --- |
| 1 | `TypeError: LineItem has no field 'sku'` (+ `[] != ['id', ...]`) | `c4cff51b` |
| 2 | `TypeError: quantize() got multiple values for argument 'exp'` | `d02c4589` |
| 3 | `AttributeError: module 'ast' has no attribute 'Num'` | `0aa2bf30` |
| 4 | `sqlite3.ProgrammingError: Binding 1 (':id') is a named parameter, but you supplied a sequence which requires nameless (qmark) placeholders.` | `e6260d31` |

(Fingerprints: `python -m unittest`, "Exit code 1" + output, Python 3.14.3, as the hook computes them. A plain
Sonnet run produced exactly these four, in this order.)

Red herrings: `shop/customer.py` works and `shop/order.py` does not. The only real difference is that
`customer.py` starts with `from __future__ import annotations`, which keeps the old behavior. Agents compare the
two classes (`Optional[...]` vs `X | None`, defaults, Decimal fields) and chase the wrong difference. The quick
workaround (add the future import to `shop/order.py`) is ruled out by TASK.md.

## Root causes and fixes (all in `recordkit/`)

1. **PEP 649/749: annotations are lazy in 3.14.** A class body no longer puts `__annotations__` into the
   namespace that the metaclass gets; it puts an `__annotate_func__` function there instead. So
   `ns.get("__annotations__", {})` in `ModelMeta.__new__` returns nothing, and every model without the future
   import has zero fields. The tempting "3.14 fix" `ns.get("__annotate__")` also returns `None` (the key is
   `__annotate_func__` in 3.14 final). Fix: create the class first, then read `inspect.get_annotations(cls)`
   (works on 3.10+) and take defaults from `cls.__dict__`; or on 3.14 use
   `annotationlib.get_annotate_from_class_namespace(ns)` with `annotationlib.call_annotate_function(...,
   annotationlib.Format.FORWARDREF)`.
2. **`functools.partial` is a method descriptor in 3.14.** `Model._cents = functools.partial(quantize, exp=...)`
   is a class attribute, so `self._cents(x)` now passes `self` too: `quantize(self, x, exp=...)`, hence
   "multiple values for argument 'exp'". (3.13 only printed a FutureWarning.) Fix:
   `_cents = staticmethod(functools.partial(quantize, exp=Decimal("0.01")))`.
3. **`ast.Num` was removed in 3.14** (deprecated since 3.8). Fix in `recordkit/rules.py`:
   `isinstance(node, ast.Constant) and type(node.value) in (int, float)`, and `node.value` instead of `node.n`.
4. **sqlite3: named placeholders with a tuple now raise** (a DeprecationWarning since 3.12, an error in 3.14).
   `store.py` builds `VALUES (:id, :lines, ...)` but passes `tuple(row.values())`. Fix: pass the `row` dict.

After all four, `python -m unittest` passes (11 tests). Verified on Python 3.14.3: each fix reveals the next
error, in the order above.
