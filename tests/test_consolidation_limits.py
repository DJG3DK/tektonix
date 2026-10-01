"""Consolidation's model gets room to answer and a budget to think in
(2026-10-01: 18,700 reasoning tokens left no room for the rewritten memory,
and a busy project used all 40 model calls before writing)."""

from agent import consolidation


def test_the_limits_leave_room_for_a_busy_day_and_a_whole_memory():
    assert consolidation.CONSOLIDATION_MODEL_CALL_LIMIT >= 80
    assert consolidation.CONSOLIDATION_TOOL_CALL_LIMIT > consolidation.CONSOLIDATION_MODEL_CALL_LIMIT
    assert consolidation.CONSOLIDATION_MAX_OUTPUT_TOKENS > 32_768, "above the router's default ceiling"
    assert consolidation.CONSOLIDATION_REASONING_TOKENS < consolidation.CONSOLIDATION_MAX_OUTPUT_TOKENS // 4
    assert consolidation.CONSOLIDATION_BUDGET_USD <= 2.0, "the dollar ceiling is what bounds cost"


def test_the_model_is_built_with_the_output_and_reasoning_caps():
    import inspect
    src = inspect.getsource(consolidation)
    assert "max_tokens=CONSOLIDATION_MAX_OUTPUT_TOKENS" in src
    assert '"reasoning": {"max_tokens": CONSOLIDATION_REASONING_TOKENS}' in src
