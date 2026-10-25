from decimal import Decimal

from shopcart.cart import Item, apply_coupon, cart_total


def test_empty_cart_is_free():
    assert cart_total([]) == Decimal("0.00")


def test_tax_is_added():
    items = [Item("mug", Decimal("12.00"), 2)]
    assert cart_total(items) == Decimal("25.74")


def test_save10_coupon():
    items = [Item("lamp", Decimal("40.00"))]
    assert cart_total(items, coupon="SAVE10") == Decimal("38.61")


def test_fiveoff_needs_25_dollars():
    assert apply_coupon(Decimal("24.99"), "FIVEOFF") == Decimal("24.99")
    assert apply_coupon(Decimal("25.00"), "FIVEOFF") == Decimal("20.00")
