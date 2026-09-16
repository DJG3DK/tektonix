"""Unit tests for agent/classify.py -- the one-shot task classifier used for
the Analytics dashboard's cost/count-by-category breakdown, and for
injecting an explicit test-coverage reminder into the goal when warranted.
Never read by anything that actually executes a task otherwise.
"""

from agent.classify import TASK_CATEGORIES, TaskClassification, classify_task


class _FakeConfig:
    router_base_url = "http://127.0.0.1:4000/v1"
    router_api_key = "test-key"


def _fake_chat_openai_class(result=None, exc=None):
    """A stand-in for ChatOpenAI whose .with_structured_output(...) returns
    a fake runnable yielding `result` (or raising `exc`) -- with_structured_
    output wraps the model in its own Runnable chain internally, so
    monkeypatching ChatOpenAI.ainvoke directly (the old approach, before
    needs_tests was added) no longer reliably intercepts the real call.
    """

    class _FakeRunnable:
        async def ainvoke(self, messages):
            if exc:
                raise exc
            return result

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, schema):
            return _FakeRunnable()

    return _FakeChatOpenAI


async def test_falls_back_to_other_on_any_failure(monkeypatch):
    monkeypatch.setattr("agent.classify.ChatOpenAI", _fake_chat_openai_class(exc=ConnectionError("router unreachable")))
    result = await classify_task("add a wishlist feature", _FakeConfig())
    assert result.category == "other"
    assert result.needs_tests is False


async def test_falls_back_to_other_on_malformed_reply(monkeypatch):
    # with_structured_output should always return a TaskClassification or
    # raise, but a fallback for a wrong-shaped reply is still worth having.
    monkeypatch.setattr("agent.classify.ChatOpenAI", _fake_chat_openai_class(result="not a TaskClassification"))
    result = await classify_task("add a wishlist feature", _FakeConfig())
    assert result.category == "other"
    assert result.needs_tests is False


async def test_falls_back_to_other_when_category_outside_taxonomy(monkeypatch):
    bad = TaskClassification(category="definitely-not-a-real-category", needs_tests=True)
    monkeypatch.setattr("agent.classify.ChatOpenAI", _fake_chat_openai_class(result=bad))
    result = await classify_task("add a wishlist feature", _FakeConfig())
    assert result.category == "other"
    assert result.needs_tests is False


async def test_accepts_a_valid_classification(monkeypatch):
    good = TaskClassification(category="feature", needs_tests=True)
    monkeypatch.setattr("agent.classify.ChatOpenAI", _fake_chat_openai_class(result=good))
    result = await classify_task("add a wishlist feature", _FakeConfig())
    assert result.category == "feature"
    assert result.needs_tests is True


def test_taxonomy_is_a_small_fixed_set():
    assert set(TASK_CATEGORIES) == {"bug-fix", "feature", "ui-styling", "performance", "investigation", "other"}
