import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pricing import discount, line_total, order_total  # noqa: E402


class DiscountRule(unittest.TestCase):
    def test_no_discount_under_50(self):
        self.assertEqual(discount(Decimal("49.99"), 5), Decimal("0.00"))

    def test_one_percent_per_year(self):
        self.assertEqual(discount(Decimal("200.00"), 3), Decimal("6.00"))

    def test_capped_at_ten_percent(self):
        self.assertEqual(discount(Decimal("100.00"), 25), Decimal("10.00"))


class Totals(unittest.TestCase):
    def test_negative_quantity_rejected(self):
        with self.assertRaises(ValueError):
            line_total(Decimal("1.00"), -1)

    def test_order_total(self):
        self.assertEqual(order_total([(Decimal("30.00"), 2)], loyalty_years=2), Decimal("58.80"))


if __name__ == "__main__":
    unittest.main()
