import unittest

from src import assets


class AssetsTest(unittest.TestCase):
    def test_reads_an_asset(self):
        self.assertEqual(assets.read_asset("logo.txt"), b"logo\n")

    def test_reads_a_nested_asset(self):
        self.assertEqual(assets.read_asset("icons/star.txt"), b"star\n")
