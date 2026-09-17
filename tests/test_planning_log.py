"""A planning turn has to be readable after the fact.

Asked on 2026-09-12 what a 78-minute planning turn was confused about, the
honest answer was that the conversation was unreadable. Both existing copies
are lossy: the checkpoint's message list is rewritten when summarization
compacts a long turn, and the in-memory buffer dies with the process -- which
on this box has meant a pm2 memory kill, a deploy, or a crash. 413 checkpoints
existed for that session and every one had an empty messages channel.

So the transcript is written to the same Postgres store as everything else:
batched, capped, and dropped when the session is.
"""

from __future__ import annotations

import asyncio

from agent import planning_log


class FakeStore:
    def __init__(self, fail: set[str] | None = None):
        self.rows: dict[tuple, dict] = {}
        self.fail = fail or set()
        self.writes = 0

    async def aget(self, ns, key):
        if "get" in self.fail:
            raise RuntimeError("store down")
        v = self.rows.get((ns, key))
        return type("Item", (), {"key": key, "value": v})() if v is not None else None

    async def aput(self, ns, key, value):
        if "put" in self.fail:
            raise RuntimeError("store down")
        self.writes += 1
        self.rows[(ns, key)] = dict(value)

    async def adelete(self, ns, key):
        self.rows.pop((ns, key), None)


def _entry(i):
    return {"kind": "assistant", "summary": f"step {i}", "detail": f"detail {i}",
            "timestamp": "2026-09-12T16:00:00Z"}


def test_entries_survive_a_flush_and_read_back_in_order():
    store = FakeStore()
    rec = planning_log.Recorder("storefront", "s1", store)
    for i in range(3):
        rec.add(_entry(i))
    assert asyncio.run(rec.flush()) == 3
    entries = asyncio.run(planning_log.load(store, "storefront", "s1"))
    assert [e["summary"] for e in entries] == ["step 0", "step 1", "step 2"]


def test_a_second_turn_appends_rather_than_replacing():
    store = FakeStore()
    first = planning_log.Recorder("r", "s1", store)
    first.add(_entry(0))
    asyncio.run(first.flush())
    second = planning_log.Recorder("r", "s1", store)
    second.add(_entry(1))
    asyncio.run(second.flush())
    assert len(asyncio.run(planning_log.load(store, "r", "s1"))) == 2


def test_writing_is_batched_not_one_store_call_per_entry():
    """A busy turn publishes several entries a second. A write each would be
    write amplification for telemetry."""
    store = FakeStore()
    rec = planning_log.Recorder("r", "s1", store)
    due = [rec.add(_entry(i)) for i in range(planning_log.FLUSH_EVERY - 1)]
    assert not any(due), "nothing is due before the batch fills"
    assert store.writes == 0
    assert rec.add(_entry(99)) is True, "the batch is now due"
    asyncio.run(rec.flush())
    assert store.writes == 1


def test_a_quiet_turn_still_flushes_on_time(monkeypatch):
    store = FakeStore()
    rec = planning_log.Recorder("r", "s1", store)
    assert rec.add(_entry(0)) is False
    monkeypatch.setattr(planning_log.time, "monotonic",
                        lambda: rec._last_flush + planning_log.FLUSH_AFTER_S + 1)
    assert rec.add(_entry(1)) is True, "a deadline flush, not only a volume one"


def test_the_transcript_is_capped(monkeypatch):
    monkeypatch.setattr(planning_log, "MAX_ENTRIES", 10)
    store = FakeStore()
    rec = planning_log.Recorder("r", "s1", store)
    for i in range(40):
        rec.add(_entry(i))
        asyncio.run(rec.flush())
    entries = asyncio.run(planning_log.load(store, "r", "s1"))
    assert len(entries) == 10
    assert entries[-1]["summary"] == "step 39", "the tail is what gets read"


def test_a_broken_store_never_breaks_the_turn():
    rec = planning_log.Recorder("r", "s1", FakeStore(fail={"put"}))
    rec.add(_entry(0))
    assert asyncio.run(rec.flush()) == 0

    rec2 = planning_log.Recorder("r", "s2", FakeStore(fail={"get"}))
    rec2.add(_entry(0))
    assert asyncio.run(rec2.flush()) == 0
    assert asyncio.run(planning_log.load(FakeStore(fail={"get"}), "r", "s1")) == []


def test_no_store_at_all_is_not_an_error():
    rec = planning_log.Recorder("r", "s1", None)
    rec.add(_entry(0))
    assert asyncio.run(rec.flush()) == 0
    assert asyncio.run(planning_log.load(None, "r", "s1")) == []


def test_deleting_a_session_takes_its_transcript():
    store = FakeStore()
    rec = planning_log.Recorder("r", "s1", store)
    rec.add(_entry(0))
    asyncio.run(rec.flush())
    asyncio.run(planning_log.forget(store, "r", "s1"))
    assert asyncio.run(planning_log.load(store, "r", "s1")) == []


def test_junk_entries_are_not_stored():
    rec = planning_log.Recorder("r", "s1", FakeStore())
    assert rec.add("not an entry") is False
    assert rec.add(None) is False
