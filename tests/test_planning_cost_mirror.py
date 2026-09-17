"""Live planning spend has to be visible before the turn ends.

Planning banked cost_usd only when a turn finished, so the session row read
0.00 for the whole turn. Any hydrate in that window -- a page reload, a
reconnect after a dropped socket -- wrote that 0.00 over whatever live figure
the stream had put on screen. Reported live 2026-08-31 on a turn that showed
$0 for 1h52m and then banked $8.11.

Build tasks already mirror live cost into their meta row for exactly this
reason; this brings planning to parity.
"""

import pytest

from agent import server


class _Item:
    def __init__(self, value):
        self.value = value


class _Store:
    def __init__(self, value=None):
        self.value = value
        self.writes = []

    async def aget(self, ns, key):
        return _Item(dict(self.value)) if self.value is not None else None

    async def aput(self, ns, key, value):
        self.writes.append(value)
        self.value = value


@pytest.mark.asyncio
async def test_it_writes_the_live_cost_onto_the_session():
    store = _Store({"session_id": "s1", "cost_usd": 0.0, "turn_active": True})
    await server._mirror_planning_cost(store, "webapp", "s1", 4.25)
    assert store.value["cost_usd"] == 4.25


@pytest.mark.asyncio
async def test_it_preserves_everything_else_on_the_row():
    """A blind write would drop the plan the session has already earned."""
    store = _Store({
        "session_id": "s1", "cost_usd": 0.0, "turn_active": True,
        "plan_markdown": "# a plan worth keeping", "title": "some title",
    })
    await server._mirror_planning_cost(store, "webapp", "s1", 1.0)
    assert store.value["plan_markdown"] == "# a plan worth keeping"
    assert store.value["title"] == "some title"
    assert store.value["turn_active"] is True


@pytest.mark.asyncio
async def test_a_missing_session_is_not_created_by_a_mirror():
    store = _Store(None)
    await server._mirror_planning_cost(store, "webapp", "gone", 1.0)
    assert store.writes == []


@pytest.mark.asyncio
async def test_a_broken_store_never_breaks_the_turn():
    class _Broken:
        async def aget(self, ns, key):
            raise RuntimeError("database is down")

    await server._mirror_planning_cost(_Broken(), "webapp", "s1", 1.0)  # must not raise


def test_the_throttle_is_small_enough_to_be_useful():
    """Rounds cost roughly $0.12 apiece, so anything up to a few cents still
    updates every round. Too large and the number would sit stale between
    them -- which is the bug this exists to fix."""
    assert 0 < server._PLANNING_COST_MIRROR_MIN_DELTA <= 0.02
