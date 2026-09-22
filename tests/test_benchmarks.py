"""The benchmark aggregates, against records whose right answer is known.

Every metric here is one somebody will quote to justify a change, so each
test fixes a small set of episodes where the correct number can be counted by
hand and asserts that exact number -- not a range, not "greater than zero".

The cases that matter most are the empty ones. A dashboard that renders "no
tasks ran" as 0% first-pass is worse than one that renders nothing, because
it sends somebody looking for a regression that did not happen.
"""
import json

import pytest

from agent import benchmarks


def ep(outcome="shipped", iterations=0, cost=1.0, verdict="READY", ts="2026-09-20T10:00:00Z"):
    return {"outcome": outcome, "iteration_count": iterations, "cost_usd": cost,
            "review_verdict": verdict, "timestamp": ts, "task_id": "t", "goal": "g"}


# --- the task-outcome half -------------------------------------------------

def test_first_pass_counts_only_episodes_that_reached_a_review():
    """A task that escalated before submitting anything is not a first-pass
    failure -- it never asked -- so it must not be in the denominator."""
    out = benchmarks.summarise_episodes([
        ep(verdict="READY", iterations=0),
        ep(verdict="CHANGES", iterations=3, outcome="shipped"),
        ep(verdict="", outcome="escalated", iterations=2),
    ])
    assert out["reviewed"] == 2
    assert out["first_pass"] == 1
    assert out["first_pass_rate"] == 50.0
    # ...while the escalation rate is over ALL tasks, including that one.
    assert out["tasks"] == 3
    assert out["escalation_rate"] == pytest.approx(33.3)


def test_ready_after_a_redo_is_not_a_first_pass():
    """READY is not enough: it is READY *without having gone round again*.

    iteration_count counts REDOS (verify_and_ship only increments it in
    _loop_back), so 1 already means the task was sent back once -- this is
    the boundary the number turns on and the easiest one to get wrong.
    """
    assert benchmarks.summarise_episodes([ep(verdict="READY", iterations=1)])["first_pass"] == 0
    assert benchmarks.summarise_episodes([ep(verdict="READY", iterations=4)])["first_pass"] == 0
    assert benchmarks.summarise_episodes([ep(verdict="READY", iterations=0)])["first_pass"] == 1


def test_verdict_case_does_not_decide_the_headline_number():
    assert benchmarks.summarise_episodes([ep(verdict="ready")])["first_pass"] == 1


def test_cost_per_shipped_ignores_the_tasks_that_did_not_ship():
    out = benchmarks.summarise_episodes([
        ep(outcome="shipped", cost=2.0), ep(outcome="shipped", cost=4.0),
        ep(outcome="escalated", cost=99.0, verdict=""),
    ])
    assert out["cost_per_shipped_median"] == 3.0
    # The overall total still counts the money actually spent.
    assert out["total_cost"] == 105.0


def test_p90_is_the_tail_not_the_median():
    """Nearest-rank: with ten tasks the 90th percentile is the ninth worst,
    so exactly one task is allowed to be above it."""
    out = benchmarks.summarise_episodes([ep(iterations=i) for i in range(1, 11)])
    assert out["iterations_median"] == 5.5
    assert out["iterations_p90"] == 9


def test_no_episodes_reads_as_unknown_not_as_zero_percent():
    out = benchmarks.summarise_episodes([])
    assert out["tasks"] == 0
    for k in ("first_pass_rate", "ship_rate", "iterations_median", "cost_median"):
        assert out[k] is None, k


def test_an_episode_missing_its_numbers_does_not_poison_the_aggregates():
    """Records are written by a long-lived system; a field that is absent or
    the wrong type must drop out of that one metric, not break the call."""
    out = benchmarks.summarise_episodes([
        {"outcome": "shipped", "review_verdict": "READY"},          # no counts at all
        {"outcome": "shipped", "iteration_count": None, "cost_usd": "free"},
        ep(iterations=2, cost=5.0, verdict="CHANGES"),
    ])
    assert out["tasks"] == 3
    assert out["iterations_median"] == 2 and out["cost_median"] == 5.0
    # The first record has a verdict and no iteration count: READY with
    # nothing saying it went round again still counts as a first pass.
    assert out["first_pass"] == 1


# --- the retrieval half ----------------------------------------------------

def test_section_reads_are_per_prompt_not_per_section_offered():
    out = benchmarks.summarise_retrieval([
        {"event": "memory_offered", "sections": ["a", "b", "c"]},
        {"event": "memory_offered", "sections": ["a", "b", "c"]},
        {"event": "memory_read", "section": "b"},
        {"event": "memory_read", "section": "b"},
        {"event": "memory_read", "section": "c"},
    ])
    assert out["memory_prompts"] == 2
    assert out["sections_offered"] == 6
    assert out["sections_read"] == 3
    assert out["section_reads_per_prompt"] == 1.5


def test_history_follow_rate_is_uses_over_queries():
    out = benchmarks.summarise_retrieval(
        [{"event": "query"}] * 4 + [{"event": "use"}] * 1)
    assert out["history_follow_rate"] == 25.0


def test_retrieval_with_no_events_is_unknown_rather_than_zero():
    out = benchmarks.summarise_retrieval([])
    assert out["section_reads_per_prompt"] is None
    assert out["history_follow_rate"] is None


def test_a_half_written_line_does_not_lose_the_whole_log(tmp_path):
    log = tmp_path / "retrieval_events.jsonl"
    log.write_text(
        json.dumps({"ts": 100.0, "event": "query"}) + "\n"
        + '{"ts": 101.0, "event": "qu\n'          # torn mid-write
        + "\n"
        + json.dumps({"ts": 102.0, "event": "use"}) + "\n")
    events = benchmarks._read_retrieval_events(0, 1000, log)
    assert [e["event"] for e in events] == ["query", "use"]


def test_events_outside_the_window_are_not_counted(tmp_path):
    log = tmp_path / "r.jsonl"
    log.write_text("".join(
        json.dumps({"ts": t, "event": "query"}) + "\n" for t in (50.0, 150.0, 250.0)))
    assert len(benchmarks._read_retrieval_events(100, 200, log)) == 1


def test_a_missing_log_is_not_an_error(tmp_path):
    assert benchmarks._read_retrieval_events(0, 1, tmp_path / "nope.jsonl") == []


# --- timestamps ------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (1_700_000_000, 1_700_000_000.0),
    ("2026-09-20T10:00:00Z", 1789898400.0),
    ("2026-09-20T10:00:00+00:00", 1789898400.0),
    ("not a date", None),
    ("", None),
    (None, None),
])
def test_timestamps_are_read_tolerantly(value, expected):
    assert benchmarks._ts(value) == expected


def test_an_unparseable_timestamp_drops_the_episode_from_both_windows():
    """Rather than landing it in whichever window epoch zero falls in."""
    assert benchmarks._ts(ep(ts="garbage")["timestamp"]) is None


# --- record shape ----------------------------------------------------------

def test_both_stored_episode_shapes_are_read():
    """Episodes go through StoreBackend, so the row is a document with the
    record in `content`; older rows are the record itself. Losing either
    shape silently shortens the history the metrics are computed over."""
    record = ep()
    assert benchmarks._episode_body(record) == record
    assert benchmarks._episode_body({"content": json.dumps(record)}) == record
    assert benchmarks._episode_body({"content": "{not json"}) is None
    assert benchmarks._episode_body("a string") is None


# --- the two windows, end to end -------------------------------------------

class FakeItem:
    def __init__(self, value):
        self.value = value
        self.namespace = ("episodes", "demo")
        self.key = str(id(self))


class FakeStore:
    """Enough of the Store to answer all_items(): one page, no offset."""
    def __init__(self, rows):
        self._rows = rows

    async def asearch(self, ns, limit=None, offset=None):
        if offset:
            return []
        return [FakeItem(r) for r in self._rows]


NOW = 2_000_000.0
DAY = 86400.0


async def _summary(rows, **kw):
    return await benchmarks.benchmark_summary(
        FakeStore(rows), ["demo"], lambda repo: ("episodes", repo),
        window_days=7, now=NOW, **kw)


@pytest.mark.asyncio
async def test_the_two_windows_are_split_at_the_right_boundary():
    rows = [
        {"content": json.dumps(ep(ts=NOW - 1 * DAY, verdict="READY", iterations=0))},
        {"content": json.dumps(ep(ts=NOW - 6 * DAY, verdict="READY", iterations=0))},
        # previous window: the same two tasks, but both needed a second pass
        {"content": json.dumps(ep(ts=NOW - 8 * DAY, verdict="CHANGES", iterations=3))},
        {"content": json.dumps(ep(ts=NOW - 13 * DAY, verdict="CHANGES", iterations=3))},
        # older than both windows -- must not appear anywhere
        {"content": json.dumps(ep(ts=NOW - 40 * DAY))},
    ]
    out = await _summary(rows)
    assert out["current"]["tasks"] == 2
    assert out["previous"]["tasks"] == 2
    assert out["current"]["first_pass_rate"] == 100.0
    assert out["previous"]["first_pass_rate"] == 0.0
    assert out["delta"]["first_pass_rate"] == 100.0
    assert out["delta"]["iterations_median"] == -3


@pytest.mark.asyncio
async def test_a_delta_against_a_window_with_no_data_is_omitted_not_zero():
    """Showing 0 would read as 'no change' for a metric that had nothing to
    change from."""
    out = await _summary([{"content": json.dumps(ep(ts=NOW - DAY))}])
    assert out["current"]["tasks"] == 1 and out["previous"]["tasks"] == 0
    assert "first_pass_rate" not in out["delta"]


@pytest.mark.asyncio
async def test_a_thin_sample_says_so():
    out = await _summary([{"content": json.dumps(ep(ts=NOW - DAY))}])
    assert out["sample_warning"]


@pytest.mark.asyncio
async def test_a_full_sample_carries_no_warning():
    rows = [{"content": json.dumps(ep(ts=NOW - (1 + i % 6) * DAY))} for i in range(12)]
    rows += [{"content": json.dumps(ep(ts=NOW - (8 + i % 6) * DAY))} for i in range(12)]
    out = await _summary(rows)
    assert out["current"]["tasks"] == 12 and out["previous"]["tasks"] == 12
    assert out["sample_warning"] is None


@pytest.mark.asyncio
async def test_retrieval_events_are_windowed_alongside_the_episodes(tmp_path):
    log = tmp_path / "r.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in [
        {"ts": NOW - 2 * DAY, "event": "memory_offered", "sections": ["a", "b"]},
        {"ts": NOW - 2 * DAY, "event": "memory_read", "section": "a"},
        {"ts": NOW - 10 * DAY, "event": "memory_offered", "sections": ["a", "b"]},
    ]))
    out = await _summary([], retrieval_log=log)
    assert out["current"]["sections_read"] == 1
    assert out["previous"]["sections_read"] == 0
    assert out["current"]["section_reads_per_prompt"] == 1.0
    assert out["previous"]["section_reads_per_prompt"] == 0.0


@pytest.mark.asyncio
async def test_an_empty_store_answers_rather_than_raising():
    out = await _summary([])
    assert out["current"]["tasks"] == 0
    assert out["delta"] == {}
    assert out["window_days"] == 7
