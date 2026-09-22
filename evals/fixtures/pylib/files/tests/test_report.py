import unittest

from src import report

ROWS = [
    {"amount": 10, "category": "a"},
    {"amount": 20, "category": "b"},
    {"amount": 30, "category": "a"},
]


class TestReport(unittest.TestCase):
    def test_total(self):
        self.assertEqual(report.total(ROWS), 60)

    def test_average(self):
        self.assertEqual(report.average(ROWS), 20)

    def test_by_category(self):
        self.assertEqual(report.by_category(ROWS)[0], ("a", 2))

    def test_top_n(self):
        self.assertEqual([r["amount"] for r in report.top_n(ROWS, 2)], [30, 20])

    def test_summarise(self):
        self.assertIn("3 rows", report.summarise(ROWS))


if __name__ == "__main__":
    unittest.main()
