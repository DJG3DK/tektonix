import unittest

from src import pricing


class PricingTest(unittest.TestCase):
    def test_simple_order(self):
        self.assertEqual(pricing.order_total([{"price": 10, "qty": 2}], 0.1), 22.0)

    def test_no_lines(self):
        self.assertEqual(pricing.order_total([], 0.2), 0)
