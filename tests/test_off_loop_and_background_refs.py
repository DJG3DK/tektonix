"""Fire-and-forget work is held until it finishes, and ledger reads happen off
the event loop.

asyncio keeps only a weak reference to a bare create_task, so a notification
or a transcript flush scheduled that way could be collected mid-flight and
silently never happen. And the router ledger is a file of up to 5MB that the
budget guard and the work node parsed on the loop thread, stalling every
other task's websocket while it did.
"""
import asyncio
import gc
import inspect
import json
import threading

import pytest

from agent import live_state, notify, task_runtime
from agent.middleware.budget_guard import BudgetGuardMiddleware, BudgetTracker
from agent.nodes import work
from agent.tools.router_ledger import RouterLedger
from langchain_core.messages import AIMessage
from langchain.agents.middleware.types import ModelResponse


async def test_fire_and_forget_holds_the_task_until_it_finishes():
    release = asyncio.Event()
    ran = []

    async def job():
        await release.wait()
        ran.append(True)

    task = live_state.fire_and_forget(job())
    task_id = id(task)
    del task
    gc.collect()
    assert any(id(t) == task_id for t in live_state.background_tasks)
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert ran == [True]
    assert not any(id(t) == task_id for t in live_state.background_tasks)


async def test_a_failed_background_job_is_retrieved_not_reported(caplog):
    async def boom():
        raise RuntimeError("telegram down")

    live_state.fire_and_forget(boom())
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()
    assert "never retrieved" not in caplog.text


def test_fire_and_forget_without_a_loop_does_not_leak_the_coroutine():
    async def job():
        pass

    coro = job()
    with pytest.raises(RuntimeError):
        live_state.fire_and_forget(coro)
    assert coro.cr_frame is None, "closed, so no 'was never awaited' warning"


async def test_notify_and_flush_are_held(monkeypatch):
    gate = asyncio.Event()

    async def slow_notify(*a, **k):
        await gate.wait()
        return 0

    class Rec:
        async def flush(self):
            await gate.wait()

    monkeypatch.setattr(notify, "notify_operators", slow_notify)
    before = set(live_state.background_tasks)
    notify.notify_operators_bg(None, "hi")
    task_runtime.flush_task_log_bg(Rec())
    gc.collect()
    assert len(live_state.background_tasks - before) == 2
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert live_state.background_tasks <= before


class _ThreadRecordingLedger(RouterLedger):
    def __init__(self, path):
        super().__init__(path)
        self.read_threads: list[threading.Thread] = []

    def _refresh(self):
        before = self._signature
        super()._refresh()
        if self._signature != before:
            self.read_threads.append(threading.current_thread())


async def test_the_budget_guard_reads_the_ledger_off_the_loop(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", lambda *a, **k: 0.30)
    log = tmp_path / "routing.jsonl"
    log.write_text(json.dumps({"call_id": "c1", "cost": 0.04}) + "\n")
    ledger = _ThreadRecordingLedger(log)
    tracker = BudgetTracker(budget_usd=10.0, ledger=ledger)
    mw = BudgetGuardMiddleware(tracker)
    msg = AIMessage(content="ok", response_metadata={"model_name": "z-ai/glm-5.2",
                                                     "headers": {"x-router-call-id": "c1"}})

    async def handler(_req):
        return ModelResponse(result=[msg])

    await mw.awrap_model_call(request=None, handler=handler)
    assert tracker.total_cost == pytest.approx(0.04)
    assert ledger.read_threads, "the ledger was read"
    assert all(t is not threading.main_thread() for t in ledger.read_threads)


def test_the_work_node_totals_the_ledger_in_a_thread():
    assert "asyncio.to_thread(_reconciled_cost, state)" in inspect.getsource(work)
