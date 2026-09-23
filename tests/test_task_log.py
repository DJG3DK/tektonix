"""A build task's transcript has to outlive the process.

Asked on 2026-09-14 why a task "got lost", the honest answer needed the
transcript and there wasn't one. What survived was a 123-character stub in
execution_log ("I verified the repo state before building, and found a
fundamental problem...") and a clean worktree. The question it asked -- the
thing that would have explained everything -- lived in pending_approval, which
stopping cleared, and the live log is in-process memory.

Planning sessions got a durable transcript on 2026-09-12. Build tasks are the
ones that run for two hours and delegate seven subagents, so they needed it
more and got it later.
"""

from __future__ import annotations

import asyncio

import pytest

from agent import planning_log


class FakeStore:
    def __init__(self):
        self.slots: dict[tuple, dict] = {}

    async def aget(self, ns, key):
        v = self.slots.get((ns, key))
        return type("Item", (), {"value": v})() if v is not None else None

    async def aput(self, ns, key, value):
        self.slots[(ns, key)] = dict(value)

    async def adelete(self, ns, key):
        self.slots.pop((ns, key), None)


def _rec(store, task_id="T1", **kw):
    return planning_log.Recorder("proj", task_id, store,
                                 namespace=planning_log.TASK_NAMESPACE,
                                 detail_cap=planning_log.TASK_DETAIL_CAP, **kw)


def _entry(i, detail=""):
    return {"node": "work", "summary": f"step {i}", "detail": detail,
            "cost_usd": 0.0, "timestamp": "2026-09-14T12:00:00Z"}


@pytest.fixture
def store():
    return FakeStore()


def test_a_task_transcript_round_trips(store):
    rec = _rec(store)
    for i in range(3):
        rec.add(_entry(i))
    asyncio.run(rec.flush())
    back = asyncio.run(planning_log.load(store, "proj", "T1",
                                         namespace=planning_log.TASK_NAMESPACE))
    assert [e["summary"] for e in back] == ["step 0", "step 1", "step 2"]


def test_tasks_and_planning_sessions_do_not_collide(store):
    """Same id, different drawer. A task and a planning session sharing an id
    must not overwrite each other."""
    task = _rec(store, task_id="SHARED")
    task.add(_entry(1, "from the task"))
    asyncio.run(task.flush())

    plan = planning_log.Recorder("proj", "SHARED", store)  # default namespace
    plan.add({"kind": "message", "text": "from the planning session"})
    asyncio.run(plan.flush())

    t = asyncio.run(planning_log.load(store, "proj", "SHARED",
                                      namespace=planning_log.TASK_NAMESPACE))
    p = asyncio.run(planning_log.load(store, "proj", "SHARED"))
    assert len(t) == 1 and t[0]["detail"] == "from the task"
    assert len(p) == 1 and p[0]["text"] == "from the planning session"


def test_long_details_are_trimmed_for_the_durable_copy(store):
    """754 tool calls at 2KB of detail each is a megabyte and a half in one
    row. The live view keeps the full text; what has to survive a restart is
    the shape of the run."""
    rec = _rec(store)
    rec.add(_entry(0, "x" * 5000))
    asyncio.run(rec.flush())
    back = asyncio.run(planning_log.load(store, "proj", "T1",
                                         namespace=planning_log.TASK_NAMESPACE))
    assert len(back[0]["detail"]) <= planning_log.TASK_DETAIL_CAP + 20
    assert back[0]["detail"].endswith("…[trimmed]")


def test_short_details_are_untouched(store):
    rec = _rec(store)
    rec.add(_entry(0, "a real conclusion"))
    asyncio.run(rec.flush())
    back = asyncio.run(planning_log.load(store, "proj", "T1",
                                         namespace=planning_log.TASK_NAMESPACE))
    assert back[0]["detail"] == "a real conclusion"


def test_a_restart_appends_rather_than_replacing(store):
    """The case the whole thing exists for: a second process continuing a
    transcript it did not start."""
    first = _rec(store)
    first.add(_entry(0))
    asyncio.run(first.flush())

    second = _rec(store)          # a fresh process
    second.add(_entry(1))
    asyncio.run(second.flush())

    back = asyncio.run(planning_log.load(store, "proj", "T1",
                                         namespace=planning_log.TASK_NAMESPACE))
    assert [e["summary"] for e in back] == ["step 0", "step 1"]


def test_deleting_a_task_takes_its_transcript(store):
    rec = _rec(store)
    rec.add(_entry(0))
    asyncio.run(rec.flush())
    asyncio.run(planning_log.forget(store, "proj", "T1",
                                    namespace=planning_log.TASK_NAMESPACE))
    assert asyncio.run(planning_log.load(store, "proj", "T1",
                                         namespace=planning_log.TASK_NAMESPACE)) == []


def test_the_server_records_hydrates_and_forgets():
    """Wiring, pinned by shape: any one of the three missing makes the feature
    silently do nothing."""
    import inspect

    import agent.server as srv

    assert "_start_task_recorder(task_id, repo)" in inspect.getsource(srv._stream_graph)
    assert "task_recorders.get(task_id)" in inspect.getsource(srv._publish)   # agent/task_runtime.py
    assert "planning_log.TASK_NAMESPACE" in inspect.getsource(srv.get_task)
    assert "planning_log.forget" in inspect.getsource(srv.delete_task)
