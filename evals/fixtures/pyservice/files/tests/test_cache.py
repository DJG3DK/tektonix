import unittest

from src.cache import LRUCache


class CacheTest(unittest.TestCase):
    def test_holds_up_to_capacity(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        self.assertEqual((c.get("a"), c.get("b")), (1, 2))

    def test_evicts_when_full(self):
        c = LRUCache(1)
        c.put("a", 1)
        c.put("b", 2)
        self.assertNotIn("a", c)
        self.assertEqual(len(c), 1)
