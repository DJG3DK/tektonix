"""How long this project's checks take, so the dashboard can tell quiet from
wedged.

Reported 2026-09-13 with a screenshot: a task on webapp sat in its check suite
while the stream said "several minutes of quiet is normal here" and the yellow
banner directly beneath said "the agent is either on a long model call or
stuck". Both true-looking, one of them wrong. The suite was healthy — 111
tests, 307s and 311s on its two runs that afternoon.

A fixed threshold cannot serve both that project and one whose suite takes
twenty seconds, so the gate measures its own runs per project.
"""

from __future__ import annotations

import asyncio

import pytest

from agent import check_timing


class FakeStore:
    def __init__(self):
        self.slots: dict[tuple, dict] = {}
        self.fail = False

    async def aget(self, ns, key):
        if self.fail:
            raise RuntimeError("store down")
        v = self.slots.get((ns, key))
        return type("Item", (), {"value": v})() if v is not None else None

    async def aput(self, ns, key, value):
        if self.fail:
            raise RuntimeError("store down")
        self.slots[(ns, key)] = dict(value)


@pytest.fixture
def store():
    return FakeStore()


def test_nothing_recorded_yet_is_a_real_answer(store):
    """None, not a guess. Inventing a number for a project we have never timed
    would be the same mistake as the fixed threshold this replaces."""
    assert asyncio.run(check_timing.expected_seconds(store, "proj")) is None


def test_one_run_is_enough_to_estimate_from(store):
    asyncio.run(check_timing.record(store, "proj", 310.4))
    assert asyncio.run(check_timing.expected_seconds(store, "proj")) == 310.4


def test_the_median_ignores_one_pathological_run(store):
    """A cold dependency install or a loaded machine must not move the number
    that decides whether a normal run looks broken."""
    for s in (300, 310, 305, 3000, 308):
        asyncio.run(check_timing.record(store, "proj", s))
    assert asyncio.run(check_timing.expected_seconds(store, "proj")) == 308.0


def test_projects_are_measured_separately(store):
    asyncio.run(check_timing.record(store, "trading-bot", 310))
    asyncio.run(check_timing.record(store, "small-site", 19))
    assert asyncio.run(check_timing.expected_seconds(store, "trading-bot")) == 310.0
    assert asyncio.run(check_timing.expected_seconds(store, "small-site")) == 19.0


def test_it_follows_a_suite_that_grows(store):
    """Only the recent runs count, so a suite that genuinely got slower stops
    being judged against what it used to be."""
    for _ in range(check_timing.KEEP):
        asyncio.run(check_timing.record(store, "proj", 60))
    for _ in range(check_timing.KEEP):
        asyncio.run(check_timing.record(store, "proj", 300))
    assert asyncio.run(check_timing.expected_seconds(store, "proj")) == 300.0


def test_the_history_is_capped(store):
    for i in range(50):
        asyncio.run(check_timing.record(store, "proj", 100 + i))
    runs = store.slots[(check_timing.NAMESPACE, "proj")]["runs"]
    assert len(runs) == check_timing.KEEP


def test_a_broken_store_never_reaches_the_caller(store):
    """Telemetry. A task must not fail because we could not write down how
    long something took."""
    store.fail = True
    asyncio.run(check_timing.record(store, "proj", 300))          # must not raise
    assert asyncio.run(check_timing.expected_seconds(store, "proj")) is None


def test_no_store_at_all_is_handled(store):
    asyncio.run(check_timing.record(None, "proj", 300))
    assert asyncio.run(check_timing.expected_seconds(None, "proj")) is None


def test_nonsense_durations_are_not_recorded(store):
    asyncio.run(check_timing.record(store, "proj", 0))
    asyncio.run(check_timing.record(store, "proj", -5))
    assert asyncio.run(check_timing.expected_seconds(store, "proj")) is None


def test_the_gate_announces_both_silent_phases():
    """The check run said something already; the review wait said nothing at
    all, and it can be quiet for the whole review_wait_timeout_s."""
    import inspect

    from agent.nodes import verify_and_ship

    checks_src = inspect.getsource(verify_and_ship._verify_and_ship_inner)
    assert '"phase": "checks"' in checks_src
    assert "check_timing.record" in checks_src, "the estimate only improves if runs are measured"
    assert "check_timing.expected_seconds" in checks_src, "the announcement has to carry it"

    # The review wait is in the ship path, not the gate's own body.
    review_src = inspect.getsource(verify_and_ship._review_and_deploy)
    assert '"phase": "review"' in review_src
    assert "wait_for_review" in review_src
