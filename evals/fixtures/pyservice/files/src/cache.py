"""A small least-recently-used cache for product lookups."""


class LRUCache:
    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._data = {}          # insertion-ordered

    def get(self, key, default=None):
        return self._data.get(key, default)

    def put(self, key, value):
        self._data[key] = value
        if len(self._data) > self.capacity:
            oldest = next(iter(self._data))
            del self._data[oldest]

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data
