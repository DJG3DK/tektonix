"""Reading a whole namespace out of a store that is not helping.

Each of these fakes is a real store's behaviour, not a hypothetical: one
caps its pages below what was asked for, one has no `offset` parameter at
all, one accepts `offset` and ignores it, and one hands back a burst of
writes that all share a timestamp. Every copy of this loop in the tree
before it was lifted here handled some of them.
"""

import datetime

from agent.store_paging import all_items, recent_items


class _Item:
    def __init__(self, key, updated_at=None, namespace=()):
        self.key = key
        self.value = {"key": key}
        self.updated_at = updated_at
        self.namespace = namespace


class PagingStore:
    """Pages honestly, but caps a page at `cap` however much was asked for."""

    def __init__(self, items, cap=10):
        self.items = items
        self.cap = cap
        self.calls = 0

    async def asearch(self, ns, limit=None, offset=0):
        self.calls += 1
        return self.items[offset:offset + min(limit or self.cap, self.cap)]


class NoOffsetStore:
    """Rejects `offset` outright, the way a store with no paging does."""

    def __init__(self, items):
        self.items = items
        self.calls = 0
        self.asked: list = []

    async def asearch(self, ns, limit=None):
        self.calls += 1
        self.asked.append(limit)
        return self.items[:limit]


class IgnoresOffsetStore:
    """Accepts `offset` and returns page one forever."""

    def __init__(self, items, cap=10):
        self.items = items
        self.cap = cap
        self.calls = 0

    async def asearch(self, ns, limit=None, offset=0):
        self.calls += 1
        return self.items[:self.cap]


def _items(n, start=0):
    return [_Item(f"k{i:04d}") for i in range(start, start + n)]


async def test_a_short_page_is_not_the_last_page():
    """A store may cap a page below the limit. Treating that as the end made
    a log with hundreds of rows read as ten."""
    store = PagingStore(_items(95), cap=10)
    got = await all_items(store, ("ns",))
    assert len(got) == 95
    assert {i.key for i in got} == {f"k{n:04d}" for n in range(95)}


async def test_a_store_with_no_offset_support_is_read_in_one_call():
    store = NoOffsetStore(_items(40))
    got = await all_items(store, ("ns",))
    assert len(got) == 40
    assert store.calls == 1, "the offset attempt never reaches the body; the fallback is the only read"


async def test_a_store_that_ignores_offset_does_not_spin_forever():
    """The de-dup is what stops this: a second page carrying nothing new
    ends the read instead of paging into eternity."""
    store = IgnoresOffsetStore(_items(50), cap=10)
    got = await all_items(store, ("ns",))
    assert len(got) == 10
    assert store.calls == 2


async def test_an_empty_namespace_reads_as_empty():
    assert await all_items(PagingStore([]), ("ns",)) == []


async def test_nothing_is_returned_twice():
    """Overlapping pages are de-duplicated by key, so a caller counting
    rows is counting rows."""
    store = IgnoresOffsetStore(_items(10), cap=10)
    got = await all_items(store, ("ns",))
    assert len({i.key for i in got}) == len(got)


# --- recent_items ----------------------------------------------------------

_T0 = datetime.datetime(2026, 9, 1, 12, 0, 0, tzinfo=datetime.UTC)


async def test_recent_items_returns_the_newest_first():
    items = [_Item(f"k{n}", _T0 + datetime.timedelta(seconds=n)) for n in range(30)]
    store = PagingStore(items, cap=7)
    got = await recent_items(store, ("ns",), 5)
    assert [i.key for i in got] == ["k29", "k28", "k27", "k26", "k25"]


async def test_recent_items_survives_a_backend_whose_timestamps_tie():
    """One-second resolution means a burst of writes shares a timestamp and
    the store falls back to insertion order -- oldest first, the reverse of
    what the caller wanted. Paging the lot is what makes the answer the same
    on both backends; within a tie, the store's own order is kept."""
    tied = [_Item(f"k{n:02d}", _T0) for n in range(20)]
    newer = [_Item("newest", _T0 + datetime.timedelta(seconds=5))]
    store = PagingStore(newer + tied, cap=6)
    got = await recent_items(store, ("ns",), 3)
    assert got[0].key == "newest"
    assert [i.key for i in got[1:]] == ["k00", "k01"]


async def test_recent_items_does_not_compare_a_naive_datetime_with_an_aware_one():
    """Postgres hands back an aware datetime and SQLite a naive one. Sorting
    a list holding both raises rather than ordering wrongly, which would
    take a whole dashboard page down."""
    mixed = [
        _Item("aware", _T0),
        _Item("naive", datetime.datetime(2026, 9, 1, 13, 0, 0)),
        _Item("none", None),
    ]
    got = await recent_items(PagingStore(mixed, cap=10), ("ns",), 3)
    assert got[-1].key == "none", "an item with no timestamp sorts oldest"
    assert len(got) == 3


async def test_two_sub_namespaces_may_share_a_key_without_losing_a_row():
    """The first argument is a namespace PREFIX, not a namespace.

    A store is free to return rows from every sub-namespace under it, and
    two sub-namespaces are each entitled to a key called "config.json". De-
    duplicating on the key alone kept one of the two and said nothing about
    the other, which is the shape of a bug nobody reports because the row
    that vanished was never seen.
    """
    rows = [
        _Item("config.json", namespace=("skills", "r", "alpha")),
        _Item("config.json", namespace=("skills", "r", "beta")),
    ]
    got = await all_items(PagingStore(rows, cap=10), ("skills", "r"))
    assert len(got) == 2
    assert {i.namespace for i in got} == {("skills", "r", "alpha"), ("skills", "r", "beta")}


async def test_a_caller_with_a_cap_of_its_own_chooses_the_no_offset_read():
    """agent/audit.py detects an overflowing log by reading past its cap, so
    the one read made to a store with no `offset` has to be its number, not
    this module's."""
    store = NoOffsetStore(_items(40))
    await all_items(store, ("ns",), no_offset_limit=8000)
    assert store.asked == [8000]
