from decimal import Decimal
from typing import Optional

from recordkit import Model, evaluate


class LineItem(Model):
    sku: str
    qty: int
    unit_price: Decimal

    def amount(self) -> Decimal:
        return self.qty * self.unit_price


class Order(Model):
    id: str
    lines: list
    tax_rule: str = "subtotal * 0.0725"
    coupon: Optional[str] = None

    def subtotal(self) -> Decimal:
        return self._cents(sum((line.amount() for line in self.lines), Decimal(0)))

    def total(self) -> Decimal:
        subtotal = self.subtotal()
        tax = self._cents(evaluate(self.tax_rule, {"subtotal": subtotal}))
        return subtotal + tax
