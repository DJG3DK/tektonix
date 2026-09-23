import unittest

from src.customers import dedupe


class DedupeTest(unittest.TestCase):
    def test_keeps_the_first_and_the_order(self):
        rows = [{"email": "a@x.com", "n": 1}, {"email": "B@x.com", "n": 2},
                {"email": "A@X.com", "n": 3}, {"email": "c@x.com", "n": 4}]
        self.assertEqual([r["n"] for r in dedupe(rows)], [1, 2, 4])
