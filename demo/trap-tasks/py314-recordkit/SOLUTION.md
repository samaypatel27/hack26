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

The code was written for Python 3.10-3.13 and is correct there. Python 3.14 changed **five** things it relies on.
They are on one code path (build the record -> export it -> price it -> save it -> read it back), so they show
up one at a time. Steps 1-4 each give a different error, so even an agent that fixes every step on the first try
fails 4 times with 4 different errors, and the hook's "whack-a-mole" signal (4+ failures, 3+ different errors)
always fires. Step 5 is the hard one: it is silent, and the obvious fixes give **the same error again**, so the
agent is still stuck on it when the hook says CALL_TEAM.

| Step | Error after the previous steps are fixed | Fingerprint |
| --- | --- | --- |
| 1 | `TypeError: LineItem has no field 'sku'` (+ `[] != ['id', ...]`) | `c4cff51b` |
| 2 | `TypeError: quantize() got multiple values for argument 'exp'` | `d02c4589` |
| 3 | `AttributeError: module 'ast' has no attribute 'Num'` | `0aa2bf30` |
| 4 | `sqlite3.ProgrammingError: Binding 1 (':id') is a named parameter, but you supplied a sequence which requires nameless (qmark) placeholders.` | `e6260d31` |
| 5 | `TypeError: 'NoneType' object is not subscriptable` (in `tests/test_store.py`: `store.get()` found no row) | `7d3b10ed` |

(Fingerprints: `python -m unittest`, "Exit code 1" + output, Python 3.14.3, as the hook computes them.)

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
   `isinstance(node, ast.Constant) and type(node.value) in (int, float)`, and `node.value` instead of `node.n`
   (`Constant.n` was removed too).
4. **sqlite3: named placeholders with a tuple now raise** (a DeprecationWarning since 3.12, an error in 3.14).
   `store.py` builds `VALUES (:id, :lines, ...)` but passes `tuple(row.values())`. Fix: pass the `row` dict.
5. **PEP 749: class annotations can no longer be read from an instance.** (What's New in Python 3.14, "Changes in
   annotations": "Accessing class annotations from an instance of an annotated class no longer works.")
   `Store.save()` stores each field by its declared type and gets the types with
   `typing.get_type_hints(record)`, on the *instance*. On 3.13 that read `record.__annotations__`, which found
   `Order`'s own annotations dict in the class. On 3.14 `Order`'s annotations are lazy and are not in
   `Order.__dict__`, so the lookup falls through to the base class: `recordkit/model.py` uses
   `from __future__ import annotations`, so `Model` still has an eager `__annotations__` dict,
   `{'_store': 'Store | None'}`. `Store` is a `TYPE_CHECKING`-only import, so evaluating it raises `NameError`,
   which `_field_types()` catches and turns into `{}` ("unknown types are stored as JSON"). So every field is
   stored as JSON: the id becomes the text `"A-1001"` with quotes, and `store.get("order", id="A-1001")` finds
   nothing, returns `None`, and the test fails on `row["total"]`.

   Why the obvious fixes fail with the same error (`7d3b10ed`):
   - `typing.get_type_hints(type(record))`: on a class it walks the MRO, evaluates `Model`'s `'Store | None'`
     too, and hits the same swallowed `NameError`.
   - `typing.get_type_hints(record, localns={"Store": Store})`: no error now, but on the instance it still
     returns only `Model`'s hints (`{'_store': ...}`), so `id`, `lines`, ... still have no type.
   - Importing `Store` for real in `model.py`: a circular import (`store.py` imports `model.py`). Even without
     that, the instance has no `__globals__`, so its string annotations can't be resolved anyway.
   - Fixing the symptom in `get()` (encode the lookup value as JSON too) would hide the bug, not fix it.

   Fix (any of these, in `_field_types()` in `recordkit/store.py`):
   - `{name: f.type for name, f in type(record).__fields__.items()}` (the types the metaclass already collected,
     after fix 1), or
   - `inspect.get_annotations(type(record))` / `annotationlib.get_annotations(type(record))` (the class's own
     annotations, without evaluating `Model`'s), or
   - `typing.get_type_hints(type(record), localns={"Store": Store})`.

After all five, `python -m unittest` passes (11 tests). Verified on Python 3.14.3: each fix reveals the next
error, in the order above, and each wrong fix for step 5 listed above gives `7d3b10ed` again.
