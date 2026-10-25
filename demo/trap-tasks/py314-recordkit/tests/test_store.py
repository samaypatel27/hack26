import json
import unittest

from recordkit import Store
from tests.test_order import sample_order


class StoreTests(unittest.TestCase):
    def test_save_order_with_total(self):
        order = sample_order()
        store = Store()
        store.save(order, total=order.total())
        row = store.get("order", id="A-1001")
        self.assertEqual(row["total"], "39.66")
        self.assertEqual(json.loads(row["lines"])[1]["sku"], "TEE-07")
