"""A pass that continues the inner thread must not republish the earlier
conversation as new log entries.

2026-09-26: after a review's loop-back, the second pass started with an
empty seen-id set, so its first state snapshot re-logged the whole first
pass -- 360 of a task's 999 entries, all stamped with the same second."""
import agent.nodes.work as work_module
from agent.outer_state import initial_state
from langchain_core.messages import AIMessage


async def _values(messages):
    yield {"messages": messages}


async def _drain(messages, seen_ids):
    written = []
    await work_module._consume_values("t1", "work", _values(messages), written.append, {}, None, seen_ids=seen_ids)
    return [e for e in written if e.get("type") == "log_entry"]


async def test_a_seeded_set_publishes_only_the_messages_the_last_pass_had_not():
    first = [AIMessage(content="reading the code", id="m1"), AIMessage(content="editing", id="m2")]
    seen: set = set()
    assert len(await _drain(first, seen)) == 2
    # The next pass continues the same thread; its first snapshot carries m1, m2 and the new m3.
    next_pass_seen = set(sorted(seen))          # what the outer state hands the next pass
    entries = await _drain(first + [AIMessage(content="fixing the finding", id="m3")], next_pass_seen)
    assert [e["entry"]["summary"] for e in entries] == ["fixing the finding"]


async def test_the_pass_hands_its_published_ids_to_the_next_via_the_state():
    assert initial_state(task_id="t", goal="g", repo="r", budget_usd=1.0)["streamed_message_ids"] == []
    assert work_module.STREAMED_IDS_MAX >= 5000
