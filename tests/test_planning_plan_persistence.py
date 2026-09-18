"""A planning turn must never erase a plan the session already has.

Confirmed live 2026-08-26: a bug-fix planning session ran 20 model calls over
19 minutes for $0.81 and finished with plan_markdown = null and no error
anywhere. The agent -- and with it plan_ref -- is rebuilt on EVERY turn, so
plan_ref always started at None; the caller then wrote that None straight over
whatever the session had saved. Any turn that merely discussed something
deleted the plan the previous turn produced.

The system prompt is explicit that re-saving every turn is NOT expected:
"Call this once the plan is genuinely ready, and again any time it
meaningfully changes." So the persistence rule has to tolerate a turn that
legitimately produces nothing.
"""
from __future__ import annotations

import pytest

from agent.tools.planning_tools import make_planning_tools


def _persist(turn_plan, prior_plan):
    """The rule as implemented in server.py's planning turn handler."""
    return turn_plan if turn_plan is not None else prior_plan


def test_a_turn_that_saves_nothing_keeps_the_existing_plan():
    assert _persist(None, "# Plan\nstep one") == "# Plan\nstep one"


def test_a_turn_that_saves_replaces_the_existing_plan():
    assert _persist("# Plan v2", "# Plan v1") == "# Plan v2"


def test_no_plan_anywhere_stays_none():
    assert _persist(None, None) is None


def test_an_empty_plan_is_still_a_deliberate_replacement():
    # "" is not None: the model explicitly saved an empty document. Treating it
    # as "nothing happened" would make a deliberate clear impossible.
    assert _persist("", "# Plan v1") == ""


def test_plan_ref_starts_from_the_seeded_draft_not_none():
    """The other half of the fix: a rebuilt agent must be able to SEE the plan
    the session already has, or it cannot refine it."""
    _tools, plan_ref = make_planning_tools("# Existing plan")
    assert plan_ref["markdown"] == "# Existing plan"


def test_plan_ref_defaults_to_none_for_a_fresh_session():
    _tools, plan_ref = make_planning_tools()
    assert plan_ref["markdown"] is None


def test_save_plan_tool_overwrites_the_seeded_draft():
    tools, plan_ref = make_planning_tools("# Old")
    save_plan = next(t for t in tools if t.name == "save_plan")
    save_plan.invoke({"markdown": "# New"})
    assert plan_ref["markdown"] == "# New"


# ---------------------------------------------------------------------------
# unsaved-plan safety net -- a plan written as chat text must not vanish
# ---------------------------------------------------------------------------
#
# Live 2026-08-27: a $6.12 planning turn compiled a full 8k-char audit report
# into its FINAL MESSAGE, never called save_plan, and ended -- plan panel
# empty, and the operator read an older plan on another session as a
# cross-save. run_planning_turn now adopts a plan-shaped final reply as the
# draft when no save_plan happened this turn.

from langchain_core.messages import AIMessage

from agent.planning_chat import run_planning_turn


PLAN_TEXT = (
    "## Build Plan\n\ntext\n\n## Steps\n\nmore\n\n## Verification\n\neven more\n\n"
    "## Rollback\n\nsteps\n" + ("detail " * 300)
)
CHAT_TEXT = "Sure -- the screener lives in scripts/ml/pairScreen.js and runs in two stages."


class _FakeAgent:
    """Just enough surface for run_planning_turn: no streaming events, one
    final state."""

    def __init__(self, final_message):
        self._final = final_message

    async def aget_state(self, _config):
        class _S:
            values = {"messages": [self._final]}
        _S.values = {"messages": [self._final]}
        return _S()

    async def astream_events(self, *_a, **_k):
        class _Run:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            @property
            def values(self):
                async def _gen():
                    if False:
                        yield None
                return _gen()
        return _Run()


async def _turn(final_message, saved=None):
    plan_ref = {"markdown": saved}
    return await run_planning_turn(_FakeAgent(final_message), plan_ref,
                                   {"configurable": {"thread_id": "t"}}, "hi", lambda e: None)


async def test_a_plan_shaped_final_reply_is_adopted_when_nothing_was_saved():
    result = await _turn(AIMessage(content=PLAN_TEXT))
    assert result is not None and result.startswith("## Build Plan")


async def test_an_ordinary_answer_is_never_adopted_as_a_plan():
    assert await _turn(AIMessage(content=CHAT_TEXT)) is None


async def test_an_explicit_save_still_wins_over_the_net():
    result = await _turn(AIMessage(content=PLAN_TEXT), saved="# The Real Plan")
    assert result == "# The Real Plan"


async def test_a_final_reply_with_tool_calls_is_not_adopted():
    msg = AIMessage(content=PLAN_TEXT, tool_calls=[{"name": "save_plan", "args": {}, "id": "c1"}])
    assert await _turn(msg) is None


async def test_block_list_content_is_extracted_before_the_heuristic():
    msg = AIMessage(content=[{"type": "text", "text": PLAN_TEXT},
                             {"type": "tool_call", "id": "x", "name": "y"}])
    result = await _turn(msg)
    assert result is not None and result.startswith("## Build Plan")


# ---------------------------------------------------------------------------
# live publish must not duplicate the operator's own bubble (2026-08-28)
# ---------------------------------------------------------------------------

from langchain_core.messages import HumanMessage as _HM


async def test_live_stream_never_publishes_user_entries():
    """The client renders its own user bubble on send and the buffer is
    seeded server-side; streaming the translated HumanMessage too painted a
    second bubble with the attachments note appended."""
    published = []

    class _Agent(_FakeAgent):
        def __init__(self, msgs):
            self._msgs = msgs

        async def aget_state(self, _config):
            # Empty BEFORE the turn (so seen_count starts at 0 and the
            # streamed messages count as new) -- run_planning_turn reads
            # state once up front to seed its dedupe counter.
            class _S: values = {"messages": []}
            return _S()

        async def astream_events(self, *_a, **_k):
            msgs = self._msgs
            class _Run:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): return False
                @property
                def values(self):
                    async def _gen():
                        yield {"messages": msgs}
                    return _gen()
            return _Run()

    msgs = [_HM(content="please fix the gridder sidebar --- ATTACHED FILES ---"),
            AIMessage(content="On it.")]
    plan_ref = {"markdown": "# saved"}
    await run_planning_turn(_Agent(msgs), plan_ref, {"configurable": {"thread_id": "t"}},
                            "please fix", lambda e: published.append(e))
    kinds = [e.get("kind") for e in published]
    assert "user" not in kinds, f"user entry leaked into the live stream: {kinds}"
    assert "agent" in kinds  # the real content still streams


async def test_live_cost_events_stream_during_a_turn():
    """Planning only reported cost at turn_complete -- a live turn showed $0
    the whole way (operator report 2026-08-28). The values loop now emits
    cost events from the shared tracker, same contract as a build task."""
    class _Tracker:
        total_cost = 0.30

    published = []

    class _Agent(_FakeAgent):
        def __init__(self):  # the base requires a final_message this test never uses
            pass

        async def aget_state(self, _config):
            class _S: values = {"messages": []}
            return _S()

        async def astream_events(self, *_a, **_k):
            class _Run:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): return False
                @property
                def values(self):
                    async def _gen():
                        _Tracker.total_cost = 0.45   # a model call landed
                        yield {"messages": [AIMessage(content="working")]}
                        _Tracker.total_cost = 0.61   # another one
                        yield {"messages": [AIMessage(content="working"), AIMessage(content="more")]}
                    return _gen()
            return _Run()

    await run_planning_turn(_Agent(), {"markdown": "# p"}, {"configurable": {"thread_id": "t"}},
                            "go", lambda e: published.append(e), tracker=_Tracker())
    costs = [e["cost_usd"] for e in published if e.get("type") == "cost"]
    assert costs == [0.45, 0.61], costs


async def test_messages_after_a_compaction_still_stream():
    """2026-09-09: SummarizationMiddleware shrank the thread below the count
    already published, `len(messages) > seen_count` went false, and a turn
    making a call every 10s streamed nothing for 13 minutes -- while the
    stall watchdog, which beats on published events, counted down to killing
    it. Publishing is by message identity now, and a tick with nothing new
    still emits a heartbeat ping."""
    published = []

    class _Agent(_FakeAgent):
        def __init__(self):
            pass

        async def aget_state(self, *_a, **_k):
            class _S:
                values = {"messages": []}
            return _S()

        async def astream_events(self, *_a, **_k):
            class _Run:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): return False
                @property
                def values(self):
                    async def _gen():
                        before = [AIMessage(content=f"step {i}", id=f"m{i}") for i in range(6)]
                        yield {"messages": before}
                        # compaction: a summary + the last two, then one new message
                        after = [_HM(content="Here is a summary of the conversation to date: ...", id="sum"),
                                 *before[-2:]]
                        yield {"messages": after}
                        yield {"messages": [*after, AIMessage(content="fresh after compaction", id="m7")]}
                    return _gen()
            return _Run()

    await run_planning_turn(_Agent(), {"markdown": "# p"}, {"configurable": {"thread_id": "t"}},
                            "hi", lambda e: published.append(e))
    texts = [e.get("summary") for e in published if e.get("kind") == "agent"]
    assert "fresh after compaction" in texts, texts
    assert texts.count("step 5") == 1, "surviving messages are not re-published after compaction"
    assert any(e.get("type") == "ping" for e in published), "a tick with nothing new still heartbeats"
