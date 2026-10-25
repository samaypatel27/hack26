"""Money helpers. All amounts are Decimal; we never use floats for money."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def quantize(value, exp=CENT):
    """Round a number to `exp` places, half up (the store's rule)."""
    return Decimal(value).quantize(exp, rounding=ROUND_HALF_UP)
