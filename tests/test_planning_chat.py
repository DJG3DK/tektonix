"""Unit tests for agent/planning_chat.py's three-seat setup:
agent-planning-chat (EASY) for the default case, escalating to
agent-planning-chat-hard only for a turn that reads as bug-fixing/debugging
or a genuinely hard problem, with agent-planning-chat-frontend sitting ahead
of that ladder for frontend sessions. What each alias resolves to is a
dashboard pin. Two separate concerns:

- classify_planning_difficulty: does the per-turn EASY/HARD call land
  correctly (keyword floor, routing through the real classify_task
  classifier -- not a second, parallel one -- and its safe fallback)?
- build_planning_agent: does `difficulty` / `route` actually pick the right
  model alias, and -- just as important -- does everything ELSE (tools,
  memory backend, permissions, system prompt) stay identical regardless, so
  the harder (or frontend) model never gets a reduced tool/memory set?
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from agent.classify import TaskClassification
from agent.planning_chat import build_planning_agent, classify_planning_difficulty


class _FakeConfig:
    router_base_url = "http://127.0.0.1:4000/v1"
    router_api_key = "test-key"


def _fake_chat_openai_class(result=None, exc=None):
    """Mirrors test_classify.py's own stand-in for classify_task's internal
    ChatOpenAI call -- with_structured_output(...) wraps the model in its
    own Runnable chain, so monkeypatching ainvoke directly wouldn't
    reliably intercept the real call.
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


# ---------------------------------------------------------------------------
# classify_planning_difficulty
# ---------------------------------------------------------------------------


async def test_keyword_floor_catches_super_hard_problem_language_without_any_model_call(monkeypatch):
    # No ChatOpenAI stand-in configured at all -- if the keyword floor
    # didn't short-circuit, this would blow up trying to actually call
    # classify_task's real, un-mocked ChatOpenAI. Not raising is itself
    # part of the proof.
    result = await classify_planning_difficulty(
        "this is a super hard problem, the race condition only happens under load", _FakeConfig()
    )
    assert result == "HARD"


async def test_routes_through_classify_task_not_a_second_parallel_classifier(monkeypatch):
    """The actual point of this redesign: reuse the SAME classifier this
    system already built for task categorization (agent/classify.py),
    rather than a separate one -- classify_task's own "bug-fix" category is
    the HARD signal."""
    monkeypatch.setattr(
        "agent.classify.ChatOpenAI",
        _fake_chat_openai_class(result=TaskClassification(category="bug-fix", needs_tests=True)),
    )
    # Deliberately no keyword-floor phrase, so this exercises the
    # classify_task path, not the keyword shortcut.
    result = await classify_planning_difficulty(
        "the checkout total is off by a cent for orders over $100, not sure why", _FakeConfig()
    )
    assert result == "HARD"


async def test_non_bug_fix_categories_are_easy(monkeypatch):
    monkeypatch.setattr(
        "agent.classify.ChatOpenAI",
        _fake_chat_openai_class(result=TaskClassification(category="ui-styling", needs_tests=False)),
    )
    result = await classify_planning_difficulty("what accent color should the hero section use?", _FakeConfig())
    assert result == "EASY"


async def test_falls_back_to_easy_on_classifier_failure(monkeypatch):
    monkeypatch.setattr(
        "agent.classify.ChatOpenAI",
        _fake_chat_openai_class(exc=ConnectionError("router unreachable")),
    )
    result = await classify_planning_difficulty("what should the color palette be?", _FakeConfig())
    assert result == "EASY"


async def test_falls_back_to_easy_on_malformed_reply(monkeypatch):
    monkeypatch.setattr(
        "agent.classify.ChatOpenAI",
        _fake_chat_openai_class(result="not a TaskClassification"),
    )
    result = await classify_planning_difficulty("what should the color palette be?", _FakeConfig())
    assert result == "EASY"


# ---------------------------------------------------------------------------
# build_planning_agent -- model selection + tool/memory parity
# ---------------------------------------------------------------------------


def _capturing_create_deep_agent(calls):
    """Stands in for deepagents.create_deep_agent: records every call's
    kwargs and returns a harmless sentinel instead of actually compiling a
    graph, so these tests stay fast and don't depend on deepagents/langgraph
    internals neither of these assertions actually need."""

    def _fake(**kwargs):
        calls.append(kwargs)
        return object()

    return _fake


async def test_easy_difficulty_uses_the_easy_alias(monkeypatch):
    calls = []
    monkeypatch.setattr("agent.planning_chat.create_deep_agent", _capturing_create_deep_agent(calls))
    checkpointer = MemorySaver()
    store = InMemoryStore()

    await build_planning_agent(_FakeConfig(), "test-repo", checkpointer, store, difficulty="EASY")

    assert len(calls) == 1
    assert calls[0]["model"].model_name == "agent-planning-chat"


async def test_hard_difficulty_uses_the_hard_alias(monkeypatch):
    calls = []
    monkeypatch.setattr("agent.planning_chat.create_deep_agent", _capturing_create_deep_agent(calls))
    checkpointer = MemorySaver()
    store = InMemoryStore()

    await build_planning_agent(_FakeConfig(), "test-repo", checkpointer, store, difficulty="HARD")

    assert len(calls) == 1
    assert calls[0]["model"].model_name == "agent-planning-chat-hard"


async def test_default_difficulty_is_easy():
    """Callers that never classify anything (get_planning_session's
    read-only state fetch never invokes the model at all) must still get a
    harmless, working agent -- default to the cheap model, not the escalated
    one."""
    checkpointer = MemorySaver()
    store = InMemoryStore()
    # Real construction, no mocking of create_deep_agent -- this is also a
    # smoke test that the whole wiring succeeds with no `difficulty` passed.
    agent, plan_ref, tracker = await build_planning_agent(_FakeConfig(), "test-repo", checkpointer, store)
    assert agent is not None
    assert plan_ref == {"markdown": None, "brief": None}


async def test_hard_tier_gets_the_identical_tools_memory_and_permissions_as_easy(monkeypatch):
    """The actual point of this whole feature: HARD must never get a
    reduced tool/memory set relative to EASY. Only `model=` may differ
    between the two difficulty branches -- everything else passed to
    create_deep_agent has to be the same.
    """
    calls = []
    monkeypatch.setattr("agent.planning_chat.create_deep_agent", _capturing_create_deep_agent(calls))
    checkpointer = MemorySaver()
    store = InMemoryStore()

    await build_planning_agent(_FakeConfig(), "test-repo", checkpointer, store, difficulty="EASY")
    await build_planning_agent(_FakeConfig(), "test-repo", checkpointer, store, difficulty="HARD")

    assert len(calls) == 2
    easy_call, hard_call = calls
    assert easy_call["model"].model_name != hard_call["model"].model_name

    # Tool identity: same tool objects (by name, since the underlying
    # functions are freshly constructed per call but must be the same set).
    easy_tool_names = sorted(t.name for t in easy_call["tools"])
    hard_tool_names = sorted(t.name for t in hard_call["tools"])
    assert easy_tool_names == hard_tool_names
    assert len(easy_tool_names) > 0

    assert easy_call["system_prompt"] == hard_call["system_prompt"]
    assert easy_call["permissions"] == hard_call["permissions"]

    # Middleware: same middleware *types*, in the same order -- the
    # instances themselves legitimately differ (each BudgetGuardMiddleware
    # wraps its own fresh BudgetTracker), so identity/equality on the list
    # itself isn't the right check; the composition must still match.
    easy_middleware_types = [type(m).__name__ for m in easy_call["middleware"]]
    hard_middleware_types = [type(m).__name__ for m in hard_call["middleware"]]
    assert easy_middleware_types == hard_middleware_types


async def test_frontend_route_uses_the_frontend_planning_alias_regardless_of_difficulty(monkeypatch):
    calls = []
    monkeypatch.setattr("agent.planning_chat.create_deep_agent", _capturing_create_deep_agent(calls))
    await build_planning_agent(_FakeConfig(), "test-repo", MemorySaver(), InMemoryStore(), difficulty="HARD", route="frontend")
    assert calls[0]["model"].model_name == "agent-planning-chat-frontend"
