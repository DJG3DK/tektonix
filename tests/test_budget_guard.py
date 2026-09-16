"""Real unit tests for BudgetTracker/BudgetGuardMiddleware's cost math and
ceiling enforcement -- the one non-negotiable hard $ requirement in this
whole system. Written during the full LangGraph/deepagents docs audit,
which found zero automated test coverage of this logic anywhere; all prior
validation was live spike-testing against real infra (real, but not
repeatable/regression-safe). Uses fakes, not mocking frameworks -- these
are pure objects with the exact attributes BudgetGuardMiddleware reads.
"""

import pytest
from langchain_core.messages import AIMessage
from langchain.agents.middleware.types import ModelResponse

from agent.middleware.budget_guard import BudgetExceededError, BudgetGuardMiddleware, BudgetTracker


def _msg(response_metadata: dict, usage_metadata: dict | None = None) -> AIMessage:
    m = AIMessage(content="hi", response_metadata=response_metadata)
    if usage_metadata is not None:
        m.usage_metadata = usage_metadata
    return m


async def _handler_returning(*messages) -> ModelResponse:
    return ModelResponse(result=list(messages))


def test_tracker_starts_at_zero_by_default():
    tracker = BudgetTracker(budget_usd=1.0)
    assert tracker.total_cost == 0.0
    assert tracker.budget_usd == 1.0


def test_tracker_seeds_starting_cost_for_resume():
    tracker = BudgetTracker(budget_usd=5.0, starting_cost=2.5)
    assert tracker.total_cost == 2.5


async def test_prefers_router_reported_cost_when_present():
    tracker = BudgetTracker(budget_usd=10.0)
    mw = BudgetGuardMiddleware(tracker)
    msg = _msg({"token_usage": {"cost": 0.0123}})

    response = await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg))

    assert response.result == [msg]
    assert tracker.total_cost == 0.0123


async def test_falls_back_to_estimate_cost_when_router_cost_absent(monkeypatch):
    tracker = BudgetTracker(budget_usd=10.0)
    mw = BudgetGuardMiddleware(tracker)
    # No "cost" key in token_usage (or no token_usage at all) -- the real
    # shape once work.py moved to astream_events(version="v3") streaming,
    # where the router's cost annotation doesn't survive.
    msg = _msg(
        {"model_name": "z-ai/glm-5.2"},
        usage_metadata={"input_tokens": 1000, "output_tokens": 200},
    )

    def fake_estimate_cost(model_name, input_tokens, output_tokens, cache_read_tokens=0):
        assert model_name == "z-ai/glm-5.2"
        assert input_tokens == 1000
        assert output_tokens == 200
        assert cache_read_tokens == 0  # no input_token_details on this message
        return 0.005

    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", fake_estimate_cost)

    await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg))

    assert tracker.total_cost == 0.005


async def test_passes_cache_read_tokens_through_to_estimate_cost(monkeypatch):
    """usage_metadata.input_token_details.cache_read must reach
    estimate_cost -- this is what makes a long, repeated-context tool-calling
    conversation get priced at the real discounted cache rate instead of
    full input price for every resent token (see model_rates.py's own
    docstring on why that distinction matters)."""
    tracker = BudgetTracker(budget_usd=10.0)
    mw = BudgetGuardMiddleware(tracker)
    msg = _msg(
        {"model_name": "z-ai/glm-5.3"},
        usage_metadata={
            "input_tokens": 118294,
            "output_tokens": 376,
            "input_token_details": {"cache_read": 117888},
        },
    )

    def fake_estimate_cost(model_name, input_tokens, output_tokens, cache_read_tokens=0):
        assert cache_read_tokens == 117888
        return 0.0329

    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", fake_estimate_cost)

    await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg))

    assert tracker.total_cost == 0.0329


async def test_sums_cost_across_multiple_messages_in_one_response():
    tracker = BudgetTracker(budget_usd=10.0)
    mw = BudgetGuardMiddleware(tracker)
    msg1 = _msg({"token_usage": {"cost": 0.01}})
    msg2 = _msg({"token_usage": {"cost": 0.02}})

    await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg1, msg2))

    assert tracker.total_cost == 0.03


async def test_trips_after_a_call_that_crosses_the_ceiling():
    tracker = BudgetTracker(budget_usd=0.05, starting_cost=0.04)
    mw = BudgetGuardMiddleware(tracker)
    msg = _msg({"token_usage": {"cost": 0.02}})  # 0.04 + 0.02 = 0.06 >= 0.05

    try:
        await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(msg))
        raised = False
    except BudgetExceededError as e:
        raised = True
        assert e.spent == 0.06
        assert e.budget == 0.05

    assert raised, "expected BudgetExceededError once the ceiling is crossed"
    # The cost of the call that crossed the ceiling is still recorded --
    # the tracker reflects real spend even on the trip, not a rollback.
    assert tracker.total_cost == 0.06


async def test_refuses_before_spending_when_already_over_budget():
    tracker = BudgetTracker(budget_usd=1.0, starting_cost=1.5)  # already over
    mw = BudgetGuardMiddleware(tracker)
    handler_called = False

    async def handler(req):
        nonlocal handler_called
        handler_called = True
        return ModelResponse(result=[])

    try:
        await mw.awrap_model_call(request=None, handler=handler)
        raised = False
    except BudgetExceededError:
        raised = True

    assert raised
    # The whole point: refuse BEFORE the call, not after -- no new spend
    # should ever be incurred once already over budget.
    assert handler_called is False


async def test_shared_tracker_aggregates_across_coordinator_and_subagent():
    """The design this whole class exists for: coordinator and every
    subagent must share ONE BudgetTracker instance so the ceiling is
    enforced against real AGGREGATE spend, not budget_usd per middleware
    instance. See budget_guard.py's own module docstring.
    """
    tracker = BudgetTracker(budget_usd=0.10)
    coordinator_mw = BudgetGuardMiddleware(tracker)
    subagent_mw = BudgetGuardMiddleware(tracker)

    await coordinator_mw.awrap_model_call(
        request=None, handler=lambda req: _handler_returning(_msg({"token_usage": {"cost": 0.06}}))
    )
    assert tracker.total_cost == 0.06

    try:
        await subagent_mw.awrap_model_call(
            request=None, handler=lambda req: _handler_returning(_msg({"token_usage": {"cost": 0.06}}))
        )
        raised = False
    except BudgetExceededError:
        raised = True

    # 0.06 + 0.06 = 0.12 >= 0.10 -- the SUBAGENT's own call trips the SAME
    # shared ceiling, proving aggregation, not independent per-instance budgets.
    assert raised
    assert tracker.total_cost == 0.12


def test_estimate_cost_strict_raises_on_unpriced_model():
    """audit C-1: for a hard ceiling, 'unknown price' must not read as $0.
    estimate_cost stays lenient (analytics); estimate_cost_strict raises so the
    budget guard fails safe."""
    from agent.tools.model_rates import (
        UnpricedModelError,
        estimate_cost,
        estimate_cost_strict,
    )
    import agent.tools.model_rates as mr
    saved = mr._rates  # restore -- this global is shared across the whole suite
    try:
        mr._rates = {"known/model": {"input": 1e-6, "output": 2e-6, "cache_read": 1e-7}}
        assert estimate_cost("who/knows", 1000, 1000) == 0.0        # lenient: unknown -> 0
        import pytest
        with pytest.raises(UnpricedModelError):                     # strict: unknown -> raise
            estimate_cost_strict("who/knows", 1000, 1000)
        assert estimate_cost_strict("known/model", 1000, 1000) > 0  # strict: known -> real
    finally:
        mr._rates = saved


# ---------------------------------------------------------------------------
# The router's billed cost replaces the estimate. On 2026-09-08 a planning
# turn was ended at "$8.09 spent" while OpenRouter had billed $1.72: the
# estimate priced 4.9M cached prompt tokens at the full input rate. The
# tracker now carries each call at its estimate only until the router's own
# figure for that call id lands in routing.jsonl (RouterLedger).
# ---------------------------------------------------------------------------

import asyncio

from agent.middleware.budget_guard import call_id_of


class FakeLedger:
    def __init__(self, billed: dict | None = None):
        self.billed = dict(billed or {})
        self.lookups = 0

    def actual_costs(self, call_ids):
        self.lookups += 1
        return {c: self.billed[c] for c in call_ids if c in self.billed}


def _streamed(call_id: str, input_tokens: int, output_tokens: int, cache_read: int = 0) -> AIMessage:
    """The real shape of a streamed call: usage counts, no router cost, and
    the proxy's call id in the response headers."""
    return _msg(
        {"model_name": "z-ai/glm-5.2", "headers": {"x-router-call-id": call_id}},
        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens,
                        "input_token_details": {"cache_read": cache_read}},
    )


def test_call_id_is_read_case_insensitively_from_response_headers():
    assert call_id_of(_msg({"headers": {"X-Router-Call-Id": "abc"}})) == "abc"
    assert call_id_of(_msg({"headers": {}})) is None
    assert call_id_of(_msg({})) is None


def test_estimate_stands_until_the_router_bills_the_call(monkeypatch):
    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", lambda *a, **k: 1.00)
    ledger = FakeLedger()
    tracker = BudgetTracker(budget_usd=10.0, ledger=ledger)
    tracker.charge(1.00, call_id="c1")
    assert tracker.total_cost == 1.00
    ledger.billed["c1"] = 0.21
    assert tracker.total_cost == 0.21, "the router's figure replaces the estimate on the next read"
    assert tracker.pending_call_ids == []


def test_charges_without_a_call_id_keep_their_estimate():
    tracker = BudgetTracker(budget_usd=10.0, ledger=FakeLedger({"whatever": 0.0}))
    tracker.charge(0.30)          # no call id: a model built without response headers
    tracker.total_cost += 0.20    # legacy direct write
    assert tracker.total_cost == pytest.approx(0.50)


async def test_guard_tags_each_call_with_its_id_and_reconciles(monkeypatch):
    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", lambda *a, **k: 0.30)
    ledger = FakeLedger()
    tracker = BudgetTracker(budget_usd=10.0, ledger=ledger)
    mw = BudgetGuardMiddleware(tracker)

    await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(_streamed("c1", 150000, 200, cache_read=148000)))
    assert tracker.total_cost == pytest.approx(0.30)
    ledger.billed["c1"] = 0.045  # what OpenRouter actually charged
    await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(_streamed("c2", 150000, 200)))
    assert tracker.total_cost == pytest.approx(0.045 + 0.30)
    assert tracker.pending_call_ids == ["c2"]


async def test_does_not_trip_on_an_estimate_the_router_contradicts(monkeypatch):
    """The 2026-09-08 failure, inverted: the estimate crosses the ceiling,
    the router's bill for the same call does not. The guard waits for the
    bill and lets the turn continue."""
    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", lambda *a, **k: 0.31)
    ledger = FakeLedger()
    tracker = BudgetTracker(budget_usd=8.0, starting_cost=7.78, ledger=ledger)
    mw = BudgetGuardMiddleware(tracker)

    async def bill_shortly():
        await asyncio.sleep(0.15)
        ledger.billed["c43"] = 0.045

    asyncio.get_running_loop().create_task(bill_shortly())
    response = await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(_streamed("c43", 154206, 321)))
    assert response.result
    assert tracker.total_cost == pytest.approx(7.78 + 0.045)


async def test_trips_when_the_router_confirms_the_estimate(monkeypatch):
    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", lambda *a, **k: 0.31)
    ledger = FakeLedger({"c1": 0.30})
    tracker = BudgetTracker(budget_usd=8.0, starting_cost=7.78, ledger=ledger)
    mw = BudgetGuardMiddleware(tracker)
    with pytest.raises(BudgetExceededError) as info:
        await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(_streamed("c1", 154206, 321)))
    assert info.value.spent == pytest.approx(8.08)
    assert "router-billed $0.3000 over 1 calls" in str(info.value)


async def test_trips_on_the_estimate_if_the_router_never_bills(monkeypatch):
    """A router on another host, or a dead log: the estimate is still a hard
    ceiling, after a bounded wait -- never an infinite one."""
    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", lambda *a, **k: 0.31)
    monkeypatch.setattr("agent.middleware.budget_guard.SETTLE_TIMEOUT_S", 0.2)
    tracker = BudgetTracker(budget_usd=8.0, starting_cost=7.78, ledger=FakeLedger())
    mw = BudgetGuardMiddleware(tracker)
    with pytest.raises(BudgetExceededError) as info:
        await mw.awrap_model_call(request=None, handler=lambda req: _handler_returning(_streamed("c1", 154206, 321)))
    assert "estimated for 1 not yet billed" in str(info.value)
