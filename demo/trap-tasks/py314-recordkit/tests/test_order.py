import unittest
from decimal import Decimal

from shop.order import LineItem, Order


def sample_order():
    return Order(id="A-1001", lines=[
        LineItem(sku="MUG-01", qty=2, unit_price=Decimal("8.495")),
        LineItem(sku="TEE-07", qty=1, unit_price=Decimal("19.99")),
    ])


class OrderTests(unittest.TestCase):
    def test_fields_in_order(self):
        self.assertEqual(list(Order.__fields__), ["id", "lines", "tax_rule", "coupon"])

    def test_defaults(self):
        order = sample_order()
        self.assertIsNone(order.coupon)
        self.assertEqual(order.tax_rule, "subtotal * 0.0725")

    def test_type_check(self):
        with self.assertRaises(TypeError):
            LineItem(sku="MUG-01", qty="two", unit_price=Decimal("8.50"))

    def test_to_dict(self):
        self.assertEqual(sample_order().to_dict()["lines"][0],
                         {"sku": "MUG-01", "qty": 2, "unit_price": "8.50"})

    def test_total(self):
        order = sample_order()
        self.assertEqual(order.subtotal(), Decimal("36.98"))
        self.assertEqual(order.total(), Decimal("39.66"))

    def test_equality(self):
        self.assertEqual(sample_order(), sample_order())
        self.assertNotEqual(sample_order(), "A-1001")
