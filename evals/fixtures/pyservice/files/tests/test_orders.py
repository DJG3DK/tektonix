import unittest
from datetime import date

from src.orders import orders_on


class OrdersTest(unittest.TestCase):
    def test_utc_shop(self):
        orders = [{"id": 1, "placed_at": "2026-03-01T10:00:00+00:00"},
                  {"id": 2, "placed_at": "2026-03-02T10:00:00+00:00"}]
        self.assertEqual([o["id"] for o in orders_on(orders, date(2026, 3, 1))], [1])
