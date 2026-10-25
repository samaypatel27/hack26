import unittest

from shop.customer import Address, Customer


class CustomerTests(unittest.TestCase):
    def test_defaults(self):
        c = Customer(email="ana@example.com", name="Ana")
        self.assertIsNone(c.address)
        self.assertEqual(c.tags, [])

    def test_nested_to_dict(self):
        c = Customer(email="ana@example.com", name="Ana",
                     address=Address(street="1 High St", city="Columbus", zip_code="43210"))
        self.assertEqual(c.to_dict()["address"], {"street": "1 High St", "city": "Columbus", "zip_code": "43210"})

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(TypeError):
            Customer(email="ana@example.com", name="Ana", phone="555")

    def test_missing_field_is_rejected(self):
        with self.assertRaises(TypeError):
            Customer(name="Ana")
