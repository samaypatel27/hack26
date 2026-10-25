# recordkit

Tiny declarative records for the shop backend. Annotate fields on a `Model` subclass and you get `__init__`,
`__repr__`, `__eq__` and `to_dict()`, a small safe evaluator for pricing rules like `subtotal * 0.0725`, and a SQLite `Store`.

`shop/` has the example models that the other teams copy from.

Run the tests from this folder:

```
python -m unittest
```
