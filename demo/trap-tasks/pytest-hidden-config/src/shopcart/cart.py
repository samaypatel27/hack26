from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Item:
    name: str
    price: Decimal
    qty: int = 1


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def apply_coupon(subtotal: Decimal, code: str | None) -> Decimal:
    """SAVE10 takes 10% off; FIVEOFF takes $5 off orders of $25 or more. Unknown codes do nothing."""
    if code == "SAVE10":
        return _money(subtotal * Decimal("0.90"))
    if code == "FIVEOFF" and subtotal >= Decimal("25"):
        return subtotal - Decimal("5")
    return subtotal


def cart_total(items: list[Item], tax_rate: Decimal = Decimal("0.0725"), coupon: str | None = None) -> Decimal:
    """Subtotal, then the coupon, then tax on the discounted amount, rounded to cents."""
    subtotal = sum((item.price * item.qty for item in items), Decimal("0"))
    discounted = apply_coupon(subtotal, coupon)
    return _money(discounted * (1 + tax_rate))
