"""One tool result, one log entry -- however many projections can see it.

Every projection of a run (`run.values` for the coordinator, `handle.values`
for each subagent) reports its WHOLE accumulated message list on every
superstep, not a delta. So whether a message gets published once or many times
is decided entirely by the already-seen set the consumer was handed.

It used to be handed a fresh one per consumer. Measured on task 279c29fd
(2026-09-12): 1328 of 1864 logged tool results were republications, the same
burst reappearing verbatim up to three times -- `{bash: 74, write: 3,
write_todos: 2}` at 17:59, 18:38 and 18:46. Every one of those also went to
the dashboard as a live log entry, which is what "the same commands are being
spammed" looked like in the task view, and it inflated every count on the
Analytics tool panel. The ratio gave it away: 4.7 tool results per model call,
where deduplicating brings it to 1.4.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent import tool_events
from agent.nodes import work


class _Projection:
    """A run.values-shaped async iterable: the whole accumulated list, every
    superstep, exactly as langgraph reports it."""

    def __init__(self, *snapshots):
        self._snapshots = snapshots

    def __aiter__(self):
        async def gen():
            for s in self._snapshots:
                yield s
        return gen()


def _tool_result(i: int) -> ToolMessage:
    return ToolMessage(content=f"exit_code=0\nout {i}", tool_call_id=f"c{i}", name="bash", id=f"m{i}")


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_events, "LOG_PATH", tmp_path / "tool_events.jsonl")
    entries: list[dict] = []
    return entries, (lambda e: entries.append(e))


def test_one_projection_publishes_each_message_once(published):
    entries, writer = published
    history = [_tool_result(0), _tool_result(1)]
    proj = _Projection({"messages": history[:1]}, {"messages": history})

    asyncio.run(work._consume_values("T1", "work", proj, writer, {}))

    logs = [e for e in entries if e["type"] == "log_entry"]
    assert len(logs) == 2, "the accumulated list must not be re-emitted each superstep"


def test_a_second_consumer_does_not_republish_what_the_first_saw(published):
    """The actual bug: a subagent handle arrives mid-run and its consumer can
    see history the root consumer already published."""
    entries, writer = published
    history = [_tool_result(i) for i in range(5)]
    shared: set = set()

    asyncio.run(work._consume_values("T1", "work", _Projection({"messages": history}),
                                     writer, {}, seen_ids=shared))
    asyncio.run(work._consume_values("T1", "work:investigator", _Projection({"messages": history}),
                                     writer, {}, seen_ids=shared))

    logs = [e for e in entries if e["type"] == "log_entry"]
    assert len(logs) == 5, f"5 results, 5 entries -- got {len(logs)}"


def test_without_the_shared_set_the_history_is_replayed(published):
    """Pins the mechanism, so nobody 'simplifies' the shared set away: given
    its own set, the second consumer republishes everything."""
    entries, writer = published
    history = [_tool_result(i) for i in range(5)]

    for _ in range(2):
        asyncio.run(work._consume_values("T1", "work", _Projection({"messages": history}),
                                         writer, {}))

    logs = [e for e in entries if e["type"] == "log_entry"]
    assert len(logs) == 10, "this is the replay the shared set exists to prevent"


def test_the_tool_log_is_written_once_per_result_too(tmp_path, monkeypatch):
    """The same bug inflated the Analytics panel, not just the live view."""
    log = tmp_path / "tool_events.jsonl"
    monkeypatch.setattr(tool_events, "LOG_PATH", log)
    history = [_tool_result(i) for i in range(3)]
    shared: set = set()

    for label in ("work", "work:investigator", "work:test-writer"):
        asyncio.run(work._consume_values("T1", label, _Projection({"messages": history}),
                                         lambda _e: None, {}, seen_ids=shared))

    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 3, f"3 tool results across 3 projections, {len(rows)} rows"
    assert {r["tool"] for r in rows} == {"bash"}


def test_an_assistant_turn_is_published_once_as_well(published):
    """Not specific to tool results -- the model's own turns were duplicated
    in the live view by the same path."""
    entries, writer = published
    history = [AIMessage(content="thinking about it", id="a1")]
    shared: set = set()

    for label in ("work", "work:investigator"):
        asyncio.run(work._consume_values("T1", label, _Projection({"messages": history}),
                                         writer, {}, seen_ids=shared))

    logs = [e for e in entries if e["type"] == "log_entry"]
    assert len(logs) == 1


def test_todos_stay_per_projection(published):
    """The shared set covers message ids only. Todos are the coordinator's and
    are compared per projection -- a subagent must not suppress the root's."""
    entries, writer = published
    todos = [{"content": "a", "status": "pending"}]
    asyncio.run(work._consume_values("T1", "work", _Projection({"todos": todos}), writer, {}))
    assert [e["type"] for e in entries] == ["todos"]


# ---------------------------------------------------------------------------
# the pass's closing prose, taken from the stream
#
# work.py used to read it back from the inner agent's checkpoint afterwards,
# where `messages` is not a channel -- so it was "" on every pass, and
# verify_and_ship read "" as "cut off mid-thought" and looped (see
# tests/test_no_diff_conclusion_loop.py). The stream is where the text
# demonstrably is.
# ---------------------------------------------------------------------------


def test_the_coordinators_closing_text_is_captured(published):
    entries, writer = published
    final: dict = {}
    msgs = [AIMessage(content="first thought", id="a1"),
            AIMessage(content="the full conclusion, several sentences long", id="a2")]

    asyncio.run(work._consume_values("T1", "work", _Projection({"messages": msgs}),
                                    writer, {}, final_text=final))

    assert final["text"] == "the full conclusion, several sentences long", "the LAST one wins"


def test_a_subagents_closing_text_is_not_the_passes_conclusion(published):
    """An investigator's sign-off is not the coordinator deciding anything, and
    the no-diff gate reads this value as a decision."""
    entries, writer = published
    final: dict = {}
    msgs = [AIMessage(content="investigation complete, here is what I found", id="s1")]

    asyncio.run(work._consume_values("T1", "work:investigator", _Projection({"messages": msgs}),
                                    writer, {}, final_text=final))

    assert final == {}


def test_an_empty_assistant_turn_does_not_overwrite_real_prose(published):
    """A tool-call-only turn carries no text; it must not erase the conclusion
    that came before it."""
    entries, writer = published
    final: dict = {}
    msgs = [AIMessage(content="the real conclusion", id="a1"),
            AIMessage(content="", id="a2")]

    asyncio.run(work._consume_values("T1", "work", _Projection({"messages": msgs}),
                                    writer, {}, final_text=final))

    assert final["text"] == "the real conclusion"


def test_block_list_content_is_flattened_not_stringified(published):
    """Kimi-style content arrives as a list of blocks; str() of that is not
    prose and would sail past the length threshold as a false conclusion."""
    entries, writer = published
    final: dict = {}
    msgs = [AIMessage(content=[{"type": "text", "text": "done: nothing to change"}], id="a1")]

    asyncio.run(work._consume_values("T1", "work", _Projection({"messages": msgs}),
                                    writer, {}, final_text=final))

    assert final["text"] == "done: nothing to change"


def test_capture_is_optional(published):
    """Callers that don't want it (every existing test) pass nothing."""
    entries, writer = published
    asyncio.run(work._consume_values("T1", "work", _Projection({"messages": [AIMessage(content="x", id="a")]}),
                                     writer, {}))


def test_the_coordinator_s_verifier_delegations_are_counted(published):
    """The ship gate sends a benchmark fix back once if the coordinator never
    asked the verifier (2026-09-24: one never did, and shipped a fix the hidden
    tests rejected). A subagent delegating is not the coordinator doing so."""
    call = AIMessage("", id="a1", tool_calls=[{"name": "task", "id": "c1",
                                              "args": {"subagent_type": "verifier", "description": "break it"}}])
    other = AIMessage("", id="a2", tool_calls=[{"name": "task", "id": "c2",
                                               "args": {"subagent_type": "test-writer", "description": "tests"}}])
    final_text: dict = {}
    asyncio.run(work._consume_values("T1", "work", _Projection({"messages": [call, other]}),
                                     lambda e: None, {}, final_text=final_text))
    assert final_text.get("verifier_calls") == 1
    sub: dict = {}
    asyncio.run(work._consume_values("T1", "work:general-purpose", _Projection({"messages": [call]}),
                                     lambda e: None, {}, final_text=sub))
    assert "verifier_calls" not in sub
