import unittest

from src.api import create_order


class ApiTest(unittest.TestCase):
    def test_a_good_order(self):
        self.assertEqual(create_order({"sku": "LAMP-1", "qty": 2}), {"ok": True, "sku": "LAMP-1", "qty": 2})

    def test_a_bad_order(self):
        self.assertFalse(create_order({"qty": 2})["ok"])
