"""Parallel `task` calls share one BudgetTracker (2026-09-29 audit, A5).

The guard's docstring used to say only one model call is ever in flight.
LangGraph runs several `task` tool calls from one message concurrently, so
that is false; what holds is weaker and is pinned here: every update to the
tracker is synchronous on the one event loop, each branch checks the ceiling
before its own call, and the overshoot is bounded by one call per branch.
"""
import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from agent.middleware.budget_guard import BudgetExceededError, BudgetGuardMiddleware, BudgetTracker


def _reply(cost: float) -> AIMessage:
    return AIMessage(content="x", response_metadata={"token_usage": {"cost": cost}})


async def test_parallel_branches_overshoot_by_at_most_one_call_each():
    tracker = BudgetTracker(budget_usd=1.0)
    guard = BudgetGuardMiddleware(tracker)
    started = 0

    async def handler(request):
        nonlocal started
        started += 1
        await asyncio.sleep(0)                   # both branches are in flight at once
        return SimpleNamespace(result=[_reply(0.6)])

    results = await asyncio.gather(guard.awrap_model_call(None, handler), guard.awrap_model_call(None, handler),
                                   return_exceptions=True)
    # Neither branch saw the other's charge before its own call: two calls ran,
    # $1.20 against a $1.00 ceiling, and the second to finish tripped it.
    assert started == 2
    assert tracker.total_cost == pytest.approx(1.2)
    assert sum(isinstance(r, BudgetExceededError) for r in results) >= 1
    # Past the ceiling, the next call from any branch is refused before it runs.
    with pytest.raises(BudgetExceededError):
        await guard.awrap_model_call(None, handler)
    assert started == 2


async def test_interleaved_charges_from_several_branches_all_count():
    tracker = BudgetTracker(budget_usd=100.0)
    guard = BudgetGuardMiddleware(tracker)

    async def handler(request):
        await asyncio.sleep(0)
        return SimpleNamespace(result=[_reply(0.25)])

    await asyncio.gather(*(guard.awrap_model_call(None, handler) for _ in range(8)))
    assert tracker.total_cost == pytest.approx(2.0)
