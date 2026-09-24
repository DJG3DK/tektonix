"""A no-diff pass has to be able to finish.

Live, 2026-09-13, task 25e2bfb0 (Dependabot alert #4): the operator watched
"work pass complete" appear every few minutes, followed by a message about the
response being cut off, followed by the agent carrying on -- twice in eight
minutes, and it would have continued to max_iterations.

Two bugs, one loop.

1. work.py read the pass's final message back from the inner agent's
   checkpoint. `messages` is not a channel there -- `aget_state` returns todos
   and no messages -- so final_summary was "" on every pass. The task's own
   execution_log shows it: `work pass complete`, detail 0 chars, twice.

2. verify_and_ship called any final response under 120 chars "cut off
   mid-thought", looped back, and reset no_diff_streak. "" is under 120, so the
   branch fired every time and the two-consecutive-no-diff "no changes needed"
   exit could never be reached. The model was told to follow through on an
   intention it had never announced.

So: an EMPTY final response is "unknown", not "cut off", and the nudge is
capped. Both are pinned here because either one alone still loops.
"""

from __future__ import annotations

import agent.nodes.verify_and_ship as vs


def _state(**over) -> dict:
    base = {
        "task_id": "T1", "goal": "fix the alert", "repo": "proj",
        "iteration_count": 0, "no_diff_streak": 0, "short_conclusion_streak": 0,
        "execution_log": [], "latest_todos": None,
    }
    base.update(over)
    return base


def _work_entry(detail: str) -> dict:
    return {"node": "work", "step_id": None, "summary": "work pass complete",
            "detail": detail, "cost_usd": 0.0, "timestamp": "2026-09-13T07:58:00Z"}


# ---------------------------------------------------------------------------
# reading the final response
# ---------------------------------------------------------------------------

def test_an_empty_final_response_is_not_a_cut_off_one():
    """The exact shape from the live loop: detail is the empty string."""
    state = _state(execution_log=[_work_entry("")])
    assert (vs._last_work_response_text(state) or "").strip() == ""


def test_a_short_response_is_still_read_as_short():
    state = _state(execution_log=[_work_entry("Looks fine.")])
    text = (vs._last_work_response_text(state) or "").strip()
    assert 0 < len(text) < vs.MIN_CONCLUSION_CHARS


def test_a_real_conclusion_clears_the_threshold():
    conclusion = (
        "I checked every manifest and lockfile in the repo: next is already at 16.3.5, which is "
        "outside the vulnerable range, so there is nothing to change for this alert."
    )
    state = _state(execution_log=[_work_entry(conclusion)])
    text = (vs._last_work_response_text(state) or "").strip()
    assert len(text) >= vs.MIN_CONCLUSION_CHARS


# ---------------------------------------------------------------------------
# the cap
# ---------------------------------------------------------------------------

def test_the_nudge_is_bounded():
    """Two is the cap, so a terse model costs two extra passes, not a task."""
    assert vs.MAX_SHORT_CONCLUSION_NUDGES == 2


def test_the_gate_counts_its_own_nudges():
    """The branch must carry the streak forward, or the cap never bites."""
    import inspect
    src = inspect.getsource(vs._verify_and_ship_inner)
    assert "short_conclusion_streak" in src
    assert "MAX_SHORT_CONCLUSION_NUDGES" in src
    # and it must not reset no_diff_streak once it stops nudging
    assert src.count("no_diff_streak=0") >= 1


def test_the_streak_is_part_of_the_state_schema():
    """A field the gate writes but the schema doesn't declare is dropped by
    langgraph, which would silently restore the unbounded loop."""
    from agent.outer_state import AgentState, initial_state
    assert "short_conclusion_streak" in AgentState.__annotations__
    fresh = initial_state(task_id="T1", goal="g", repo="proj", budget_usd=1.0)
    assert fresh["short_conclusion_streak"] == 0


def test_an_empty_response_does_not_take_the_nudge_branch():
    """The condition itself, evaluated exactly as the gate evaluates it: an
    empty response must fall through to the ordinary no-diff path, which is
    what lets a no-changes-needed task finish."""
    for text, should_nudge in (("", False), ("Looks fine.", True), ("x" * 200, False)):
        last = text.strip()
        looks_incomplete = bool(last) and len(last) < vs.MIN_CONCLUSION_CHARS
        assert looks_incomplete is should_nudge, text[:20]


def test_past_the_cap_the_branch_is_skipped_however_short_the_text():
    for nudges, should_nudge in ((0, True), (1, True), (2, False), (5, False)):
        last = "Looks fine."
        looks_incomplete = bool(last) and len(last) < vs.MIN_CONCLUSION_CHARS
        takes_branch = looks_incomplete and nudges < vs.MAX_SHORT_CONCLUSION_NUDGES
        assert takes_branch is should_nudge, nudges


# ---------------------------------------------------------------------------
# an announced action is not a conclusion, however long (2026-09-24)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.mark.parametrize("text,unfinished", [
    ("The reminder is right that I haven't written the fix yet -- I was still pinning down the exact "
     "code paths. I have everything I need now: the misleading message comes from the elif branch of "
     "_check_required_columns. Implementing now.", True),
    ("I traced the call path. Let me read core.py and then make the change.", True),
    ("Next, I'll add the regression test.", True),
    ("I investigated thoroughly. The behaviour described is already correct on this branch, so no "
     "changes are needed.", False),
    ("Fixed the regex in card.py and verified the reproduction and its neighbours; all related tests pass.",
     False),
])
def test_a_long_message_that_ends_by_announcing_work_is_not_a_conclusion(text, unfinished):
    """A benchmark task ended "done, no changes" on exactly the first of these:
    long enough for the length check, and a promise, not an answer."""
    assert vs.announces_unfinished_work(text) is unfinished


def test_a_benchmark_task_never_ends_with_no_changes(monkeypatch):
    """Its statement is to change the source; an empty patch always fails it."""
    import agent.config as cfg
    monkeypatch.setitem(cfg.PROJECTS, "bench", {"sandbox": "/tmp/bench", "benchmark": True})
    state = _state(repo="bench", no_diff_streak=1,
                   execution_log=[_work_entry("I read everything and I am confident in the analysis above. " * 3)])
    src = __import__("inspect").getsource(vs)
    assert 'benchmark task with no diff -- it requires a source change' in src
    assert (cfg.PROJECTS.get(state["repo"]) or {}).get("benchmark")
