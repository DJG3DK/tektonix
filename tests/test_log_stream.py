"""Entry identity and event ordering for the task stream.

The log had two sources -- the live socket and the REST snapshot -- and no way
to tell whether an entry from one was the same entry from the other, so both
ends kept "whichever list is longer". That loses entries in one direction and
duplicates them in the other. These tests pin the merge that replaced it.
"""
import agent.server as srv
from agent import log_stream


def entry(ts, summary, node="work", detail="", cost=0.0):
    return {"timestamp": ts, "node": node, "step_id": None, "summary": summary,
            "detail": detail, "cost_usd": cost}


def test_the_same_entry_gets_the_same_id_from_either_source():
    live = entry("2026-09-11T10:00:00Z", "ran the tests")
    # the same entry as the checkpoint writes it: same content, different dict
    durable = dict(live)
    assert log_stream.entry_id(live) == log_stream.entry_id(durable)


def test_different_entries_get_different_ids():
    ids = {
        log_stream.entry_id(entry("2026-09-11T10:00:00Z", "a")),
        log_stream.entry_id(entry("2026-09-11T10:00:00Z", "b")),
        log_stream.entry_id(entry("2026-09-11T10:00:01Z", "a")),
        log_stream.entry_id(entry("2026-09-11T10:00:00Z", "a", node="verify_and_ship")),
        log_stream.entry_id(entry("2026-09-11T10:00:00Z", "a", detail="x")),
    }
    assert len(ids) == 5


def test_a_field_added_later_does_not_change_an_entrys_identity():
    """The browser holds entries by id; a new UI hint on the server must not
    make every held entry look like a different one."""
    base = entry("2026-09-11T10:00:00Z", "ran the tests")
    assert log_stream.entry_id(base) == log_stream.entry_id({**base, "ui_hint": "collapse"})


def test_stamping_copies_rather_than_mutating_the_checkpoint():
    original = entry("2026-09-11T10:00:00Z", "ran the tests")
    stamped = log_stream.stamp([original])
    assert stamped[0]["id"]
    assert "id" not in original, "stamp() wrote into the caller's dict"


def test_an_explicit_id_is_kept():
    assert log_stream.stamp([{**entry("t", "s"), "id": "given"}])[0]["id"] == "given"


def test_merge_keeps_durable_order_and_appends_what_only_the_buffer_has():
    a, b, c = entry("t1", "a"), entry("t2", "b"), entry("t3", "c")
    merged = log_stream.merge([a, b], [b, c])
    assert [e["summary"] for e in merged] == ["a", "b", "c"]
    assert len({e["id"] for e in merged}) == 3


def test_merge_never_shrinks_the_log():
    """The concrete symptom: a mid-pass snapshot is SHORTER than what the
    browser already has, because the graph only writes execution_log when a
    pass returns."""
    live = [entry(f"t{i}", f"live {i}") for i in range(5)]
    stale_snapshot = live[:2]
    merged = log_stream.merge(stale_snapshot, live)
    assert [e["summary"] for e in merged] == [f"live {i}" for i in range(5)]


def test_merge_keeps_history_the_browser_never_saw():
    """The other direction: a snapshot holding everything from before the
    browser connected must not be thrown away for being shorter than... it
    is not, but it must also not be discarded when the buffer has more."""
    before = [entry(f"t{i}", f"old {i}") for i in range(3)]
    since = [entry(f"t{i}", f"new {i}") for i in range(3, 9)]
    merged = log_stream.merge(before, since)
    assert [e["summary"] for e in merged] == [f"old {i}" for i in range(3)] + [f"new {i}" for i in range(3, 9)]


def test_merge_is_idempotent_so_a_replayed_frame_is_harmless():
    entries = [entry("t1", "a"), entry("t2", "b")]
    once = log_stream.merge(entries, entries)
    twice = log_stream.merge(once, entries)
    assert once == twice
    assert len(once) == 2


def test_merge_handles_empty_and_junk_without_raising():
    assert log_stream.merge(None, None) == []
    assert log_stream.merge([], None) == []
    assert [e["summary"] for e in log_stream.merge(None, [entry("t", "only live")])] == ["only live"]
    assert log_stream.merge([entry("t", "a"), "not a dict", None], []) == log_stream.stamp([entry("t", "a")])


def test_the_server_hydrate_rule_is_the_merge():
    """_fuller_log is what the REST snapshot uses; it must no longer be
    'whichever is longer'."""
    live = [entry(f"t{i}", f"live {i}") for i in range(4)]
    durable = live[:1]
    merged = srv._fuller_log(live, durable)
    assert [e["summary"] for e in merged] == [f"live {i}" for i in range(4)]


def test_event_ids_are_monotonic_per_task_and_independent_between_tasks():
    seq = log_stream.SeqCounter()
    assert [seq.next("a") for _ in range(3)] == [1, 2, 3]
    assert seq.next("b") == 1, "a busy neighbour must not move this task's numbers"
    assert seq.next("a") == 4


def test_current_reports_the_last_id_without_consuming_one():
    seq = log_stream.SeqCounter()
    assert seq.current("a") == 0, "nothing published yet"
    seq.next("a")
    seq.next("a")
    assert seq.current("a") == 2
    assert seq.current("a") == 2, "current() consumed an id"
    assert seq.next("a") == 3, "current() broke the sequence"


def _fresh_stream_state(monkeypatch, max_keys=None):
    """Fresh buffers, in agent/task_runtime.py where publish() reads them AND
    on server.py's aliases, so both sides still hold the same objects."""
    from agent import live_state, task_runtime
    fresh = {"_live_task_log": {}, "_task_event_seq": log_stream.SeqCounter()}
    monkeypatch.setattr(task_runtime, "live_task_log", fresh["_live_task_log"])
    monkeypatch.setattr(task_runtime, "task_event_seq", fresh["_task_event_seq"])
    for name, obj in fresh.items():
        monkeypatch.setattr(srv, name, obj)
    monkeypatch.setattr(live_state, "subscribers", {})
    monkeypatch.setattr(srv, "_subscribers", live_state.subscribers)
    if max_keys is not None:
        monkeypatch.setattr(task_runtime, "LIVE_LOG_MAX_KEYS", max_keys)


def test_publishing_stamps_entries_and_numbers_events(monkeypatch):
    _fresh_stream_state(monkeypatch)

    event = {"execution_log": [entry("t1", "a")]}
    srv._publish("task-1", event)
    assert event["execution_log"][0]["id"]
    assert event["seq"] == 1

    second = {"status": "running"}
    srv._publish("task-1", second)
    assert second["seq"] == 2

    # A heartbeat is not an event: numbering it would make the browser think
    # it had missed content when it replays its buffer.
    ping = {"type": "ping"}
    srv._publish("task-1", ping)
    assert "seq" not in ping
    assert srv._publish("task-1", {"status": "done"}) is None
    assert srv._task_event_seq.current("task-1") == 3


# ---------------------------------------------------------------------------
# Residuals found on review (2026-09-11)
# ---------------------------------------------------------------------------

def test_the_counter_is_forgotten_with_the_live_log_it_belongs_to(monkeypatch):
    """One counter per task, kept for the life of the process, is a leak: the
    live log evicts at a cap and the counter did not."""
    _fresh_stream_state(monkeypatch, max_keys=3)

    for i in range(5):
        srv._publish(f"task-{i}", {"execution_log": [entry(f"t{i}", f"entry {i}")]})

    assert len(srv._live_task_log) == 3
    # the evicted tasks' counters went with them
    assert set(srv._task_event_seq._counters) == set(srv._live_task_log)


def test_deleting_a_task_forgets_its_stream_state():
    """Whatever else delete_task does, it must drop the in-process stream
    bookkeeping -- nothing will ever stream for that task again."""
    import inspect
    source = inspect.getsource(srv.delete_task)
    assert "_live_task_log.pop" in source
    assert "_task_event_seq.forget" in source


def test_forget_lets_a_key_start_over():
    seq = log_stream.SeqCounter()
    seq.next("a")
    seq.next("a")
    seq.forget("a")
    assert seq.current("a") == 0
    assert seq.next("a") == 1
