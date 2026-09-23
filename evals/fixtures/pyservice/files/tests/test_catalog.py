import unittest

from src import catalog


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.conn = catalog.connect()
        catalog.add_product(self.conn, "Desk Lamp", "lighting", 3500, 4)
        catalog.add_product(self.conn, "Floor Lamp", "lighting", 8900, 1)
        catalog.add_product(self.conn, "Oak Desk", "furniture", 42000, 2)

    def test_search_finds_by_name_cheapest_first(self):
        self.assertEqual(catalog.search(self.conn, "Lamp"), [("Desk Lamp", 3500), ("Floor Lamp", 8900)])

    def test_in_category(self):
        self.assertEqual(catalog.in_category(self.conn, "furniture"), [("Oak Desk",)])
