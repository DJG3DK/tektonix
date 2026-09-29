"""The planning stream numbers its events and stamps its entries, the way the
task stream does (agent/log_stream.py), so the page can open its socket before
it hydrates and merge the two by position rather than by log length
(2026-09-29 audit, U4)."""
from __future__ import annotations

import asyncio

from agent import server, task_runtime


def test_planning_events_carry_a_seq_and_entries_an_id():
    sid = "sess-seq-test"
    q: asyncio.Queue = asyncio.Queue()
    server._planning_subscribers[sid] = [(q, None)]
    try:
        entry = {"kind": "agent", "summary": "thinking", "detail": "about it", "timestamp": "2026-09-29T00:00:00Z"}
        server._publish_planning(sid, {"type": "log_entry", "entry": dict(entry)})
        server._publish_planning(sid, {"type": "ping"})
        server._publish_planning(sid, {"type": "closed"})
        first, ping, closed = (q.get_nowait() for _ in range(3))

        assert first["seq"] == 1 and closed["seq"] == 2, "content events are numbered in order"
        assert "seq" not in ping, "a heartbeat is not a position in the stream"
        assert first["entry"]["id"], "the entry is stamped so the snapshot and the socket agree on it"
        assert task_runtime.live_planning_log[sid][0]["id"] == first["entry"]["id"]
        # What the REST snapshot reports as its position: everything at or
        # below it is already in the snapshot's log.
        assert task_runtime.planning_event_seq.current(sid) == 2
    finally:
        server._planning_subscribers.pop(sid, None)
        task_runtime.live_planning_log.pop(sid, None)
        task_runtime.planning_event_seq.forget(sid)
