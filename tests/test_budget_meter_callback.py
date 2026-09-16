"""BudgetMeterCallback: spend from models invoked OUTSIDE the graph's model
node must still land in the shared BudgetTracker.

SummarizationMiddleware ainvoke()s its summary model directly inside
_acreate_summary -- no agent middleware wraps that call, so
BudgetGuardMiddleware never sees it. Measured 2026-08-27: $0.48 of $16.43
over 48h (2.9% of all router spend) uncounted. The callback rides on the
model object itself, so it fires however the model is invoked.
"""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from agent.middleware.budget_guard import BudgetMeterCallback, BudgetTracker


def _result(*msgs) -> LLMResult:
    return LLMResult(generations=[[ChatGeneration(message=m) for m in msgs]])


def _msg(model="moonshotai/kimi-k3", inp=1000, out=100, router_cost=None, cache_read=0):
    meta = {"model_name": model}
    if router_cost is not None:
        meta["token_usage"] = {"cost": router_cost}
    return AIMessage(
        content="s",
        response_metadata=meta,
        usage_metadata={"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
                        "input_token_details": {"cache_read": cache_read}},
    )


async def test_router_cost_lands_in_the_tracker():
    tracker = BudgetTracker(budget_usd=5.0)
    await BudgetMeterCallback(tracker).on_llm_end(_result(_msg(router_cost=0.07)))
    assert tracker.total_cost == pytest.approx(0.07)


async def test_falls_back_to_rate_estimation_like_the_guard_does(monkeypatch):
    tracker = BudgetTracker(budget_usd=5.0)
    calls = {}

    def fake_estimate(model, inp, out, cache_read_tokens=0):
        calls.update(model=model, inp=inp, out=out, cache=cache_read_tokens)
        return 0.0123

    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", fake_estimate)
    await BudgetMeterCallback(tracker).on_llm_end(_result(_msg(inp=5000, out=250, cache_read=100)))
    assert tracker.total_cost == pytest.approx(0.0123)
    assert calls == {"model": "moonshotai/kimi-k3", "inp": 5000, "out": 250, "cache": 100}


async def test_an_unpriced_model_charges_the_placeholder_not_zero(monkeypatch):
    """Mirrors audit C-1 on the guard: $0 against a hard ceiling is the exact
    failure that let 1.4M tokens through a $5 cap."""
    from agent.tools.model_rates import UnpricedModelError

    def raise_unpriced(*a, **k):
        raise UnpricedModelError("mystery-model")

    monkeypatch.setattr("agent.middleware.budget_guard.estimate_cost_strict", raise_unpriced)
    tracker = BudgetTracker(budget_usd=5.0)
    await BudgetMeterCallback(tracker).on_llm_end(_result(_msg(out=1000)))
    assert tracker.total_cost == pytest.approx(0.02)


async def test_accumulates_across_calls_on_the_shared_tracker():
    tracker = BudgetTracker(budget_usd=5.0, starting_cost=1.0)
    cb = BudgetMeterCallback(tracker)
    await cb.on_llm_end(_result(_msg(router_cost=0.05)))
    await cb.on_llm_end(_result(_msg(router_cost=0.02)))
    assert tracker.total_cost == pytest.approx(1.07)


async def test_a_result_with_no_usage_adds_nothing_and_does_not_raise():
    tracker = BudgetTracker(budget_usd=5.0)
    bare = AIMessage(content="no usage metadata at all")
    await BudgetMeterCallback(tracker).on_llm_end(_result(bare))
    assert tracker.total_cost == 0.0


async def test_meters_a_direct_ainvoke_the_way_summarization_calls_it():
    """End to end through a real BaseChatModel: GenericFakeChatModel emits
    usage when invoked directly -- the exact path SummarizationMiddleware
    uses -- and the callback must catch it with no middleware anywhere."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    tracker = BudgetTracker(budget_usd=5.0)
    msg = _msg(router_cost=0.03)
    model = GenericFakeChatModel(messages=iter([msg]), callbacks=[BudgetMeterCallback(tracker)])
    await model.ainvoke("summarize all of this")
    assert tracker.total_cost == pytest.approx(0.03)


def test_both_agents_attach_the_meter_to_their_summarizer():
    """Source-level pin: the summarizer role must never again be constructed
    without the meter."""
    import pathlib
    for f in ("agent/deep_agent.py", "agent/planning_chat.py"):
        src = pathlib.Path(f).read_text()
        line = next(text for text in src.splitlines()
                    if '"agent-summarizer"' in text and "llm_for_role" in text)
        assert "BudgetMeterCallback" in line, f"{f} builds an unmetered summarizer: {line.strip()}"
