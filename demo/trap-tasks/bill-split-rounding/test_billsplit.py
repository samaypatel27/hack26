import unittest

from billsplit import share


class ShareTests(unittest.TestCase):
    def test_even_split(self):
        self.assertEqual(share(10.00, 4), "$2.50")

    def test_split_with_tip(self):
        self.assertEqual(share(100, 3, tip_percent=18), "$39.33")

    def test_half_cent_rounds_up(self):
        self.assertEqual(share(10.05, 2), "$5.03")

    def test_half_cent_rounds_up_small(self):
        self.assertEqual(share(0.25, 2), "$0.13")

    def test_half_cent_rounds_up_one_dollar(self):
        self.assertEqual(share(2.01, 2), "$1.01")

    def test_half_cent_after_tip(self):
        self.assertEqual(share(10.10, 1, tip_percent=15), "$11.62")

    def test_needs_people(self):
        with self.assertRaises(ValueError):
            share(10, 0)


if __name__ == "__main__":
    unittest.main()
