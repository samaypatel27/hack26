# Solution (for the demo team only; setup_trap.py never copies this file)

## What the agent sees

`python -m unittest` runs, but 3 of 7 tests fail. Nothing crashes; the answers are one cent too low:

```
AssertionError: '$11.61' != '$11.62'     (test_half_cent_after_tip: share(10.10, 1, tip_percent=15))
AssertionError: '$1.00' != '$1.01'       (test_half_cent_rounds_up_one_dollar: share(2.01, 2))
AssertionError: '$0.12' != '$0.13'       (test_half_cent_rounds_up_small: share(0.25, 2))
FAILED (failures=3)
```

Error fingerprint (as the hook computes it): `df32df8a`. Numbers are dropped from error fingerprints, so this stays
the same while the same 3 tests fail, even if the amounts change.

## Why it is a trap

The failures point at the formatting in `format_money()`, so agents "fix the rounding" there. Every common
rounding fix fails at least one test, because the damage is already done before `format_money()` runs:

| Attempt in `format_money()` | Result | Output fingerprint |
| --- | --- | --- |
| `round(amount, 2)` | same 3 failures, **identical output** | same as the original (signal 3: "edit, run, same output") |
| `Decimal(amount).quantize(..., ROUND_HALF_UP)` | 2 failures | changes |
| `math.floor(amount * 100 + 0.5) / 100` | 2 failures | changes |
| `Decimal(str(amount)).quantize(..., ROUND_HALF_UP)` | 1 failure (`share(10.10, 1, 15)`) | changes |

## Root cause

The math in `share()` is done with binary floats. `10.10 * 1.15` is `11.614999999999998`, not `11.615`, and
`2.01 / 2` is stored just under `1.005`. By the time the number reaches `format_money()`, the half cent is
already gone. Python's `round()` and `:.2f` also use round-half-to-even on the exact binary value, so `0.125`
becomes `0.12`.

## Fix

Do the money math in `Decimal`, built from the decimal text of the inputs, and round half up once at the end:

```python
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")

def share(total, people, tip_percent=0):
    if people < 1:
        raise ValueError("people must be at least 1")
    with_tip = Decimal(str(total)) * (1 + Decimal(str(tip_percent)) / 100)
    each = with_tip / people
    return format_money(each)

def format_money(amount):
    return f"${amount.quantize(CENT, rounding=ROUND_HALF_UP)}"
```

Then all 7 tests pass. Verified on Python 3.14 (standard library only, no pytest needed).
