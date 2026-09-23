"""A plan counter that cannot go backwards.

Live, 2026-09-12, task 01e640ef: twelve items with six completed became a
fresh six-item list with nothing completed, because the coordinator answered
"update your plan" by writing what was LEFT -- which is a legitimate use of a
tool whose contract is to replace the list. The worktree already held 23
changed files and 967 deleted lines. What the operator saw was 6/12 fall to
0/6, twice, and the reasonable conclusion was that the task was looping.

The strip is not the only reader. The commit gate reads latest_todos to decide
whether the plan is finished, and incomplete_plan_streak escalates a task
whose plan never completes -- so an all-pending rewrite can hold a finished
task open. Both reasons to merge rather than replace.
"""

from agent.plan_progress import merge_todos


def counts(todos):
    """(completed, total) -- what the plan strip shows (PlanTracker counts
    done steps over plan length). A test helper: the backend never needed
    it, so it no longer lives in agent/plan_progress.py."""
    items = [t for t in todos if isinstance(t, dict)] if isinstance(todos, list) else []
    return (sum(1 for t in items if t.get("status") == "completed"), len(items))


def _t(content, status="pending"):
    return {"content": content, "status": status}


def test_the_live_shape_a_rewrite_that_drops_what_was_finished():
    before = [_t("core removal", "completed"), _t("mirror in backtester", "completed"),
              _t("frontend types", "in_progress"), _t("docs", "pending")]
    after = [_t("frontend types", "in_progress"), _t("docs", "pending"),
             _t("regenerate fixtures", "pending")]

    merged = merge_todos(before, after)

    assert counts(merged) == (2, 5), "the two finished items survive the rewrite"
    assert [t["content"] for t in merged[:2]] == ["core removal", "mirror in backtester"]
    assert [t["content"] for t in merged[2:]] == [t["content"] for t in after], \
        "and the model's own list follows, in its order"


def test_an_item_that_comes_back_as_pending_stays_completed():
    """The status is the one thing a rewrite does not get to undo."""
    before = [_t("delete the sweep module", "completed")]
    merged = merge_todos(before, [_t("delete the sweep module", "pending")])
    assert merged == [{"content": "delete the sweep module", "status": "completed"}]


def test_rewording_and_reordering_are_the_models_to_make():
    before = [_t("a", "completed"), _t("b", "pending")]
    after = [_t("b", "in_progress"), _t("a", "completed"), _t("c", "pending")]
    merged = merge_todos(before, after)
    assert [t["content"] for t in merged] == ["b", "a", "c"], "the model's order wins"
    assert counts(merged) == (1, 3)


def test_matching_ignores_whitespace_and_case():
    before = [_t("Regenerate  tests/fixtures/baseline_trades.json", "completed")]
    after = [_t("regenerate tests/fixtures/baseline_trades.json")]
    assert merge_todos(before, after)[0]["status"] == "completed"


def test_a_genuinely_new_plan_is_not_invented_around():
    """Only completed items are carried. A pending item the model dropped is
    dropped -- re-planning is allowed, un-finishing is not."""
    before = [_t("old idea", "pending"), _t("done thing", "completed")]
    merged = merge_todos(before, [_t("new idea")])
    assert [t["content"] for t in merged] == ["done thing", "new idea"]


def test_nothing_written_this_turn_leaves_the_plan_alone():
    before = [_t("a", "completed")]
    assert merge_todos(before, None) == before


def test_the_first_plan_passes_through_untouched():
    first = [_t("a"), _t("b", "in_progress")]
    assert merge_todos(None, first) == first
    assert merge_todos([], first) == first


def test_malformed_entries_never_raise():
    before = [_t("a", "completed"), "not a dict", {"no_content": True}]
    after = ["junk", _t("a"), {"content": "", "status": "pending"}]
    merged = merge_todos(before, after)
    assert any(isinstance(t, dict) and t.get("content") == "a"
               and t["status"] == "completed" for t in merged)


def test_the_denominator_never_shrinks_across_a_sequence_of_rewrites():
    """The property the operator actually cares about: twelve items, finished
    a few at a time, each rewrite listing only the rest."""
    plan = [_t(f"item {i}") for i in range(12)]
    state = merge_todos(None, plan)
    for batch in range(0, 12, 3):
        remaining = [_t(f"item {i}") for i in range(batch + 3, 12)]
        state = merge_todos(
            [{**t, "status": "completed"} if t["content"] in
             {f"item {i}" for i in range(batch + 3)} else t for t in state],
            remaining,
        )
        done, total = counts(state)
        assert total == 12, f"the plan shrank to {total} after batch {batch}"
        assert done == batch + 3, f"progress read {done} after finishing {batch + 3}"


# ---------------------------------------------------------------------------
# Rewording is not a new item
#
# Live on 2026-09-14, task aa457790: a plan that started at 11 items reached 46
# while the work went fine -- 28 files written, mtCore.js at 808 lines. None of
# the 46 were byte-identical, but sixteen were near-duplicates of another,
# because each rewrite reworded the completed items slightly and exact matching
# kept the old one alongside its own replacement:
#
#   "Provide src/strategies/modules/mtCore.js — regime classifier, 4 engine..."
#   "Write src/strategies/multiTrader.js — evalEntry (closed-bar, paper boun..."
#   "src/strategies/modules/mtCore.js — regime classifier, 4 engine detectors"
#
# The counter was honest about every item it held. It just held the same work
# four times.
# ---------------------------------------------------------------------------

MTCORE_VARIANTS = [
    "Provide src/strategies/modules/mtCore.js — regime classifier, 4 engine detectors, priority router",
    "Provide src/strategies/modules/mtCore.js — regime classifier, 4 engine detectors, priority routing",
    "src/strategies/modules/mtCore.js — regime classifier, 4 engine detectors, priority router + sizing",
]


def test_a_reworded_completed_item_is_not_kept_twice():
    previous = [{"content": MTCORE_VARIANTS[0], "status": "completed"}]
    incoming = [{"content": MTCORE_VARIANTS[1], "status": "completed"}]
    merged = merge_todos(previous, incoming)
    assert len(merged) == 1, [t["content"] for t in merged]


def test_the_plan_stops_growing_across_repeated_rewordings():
    """The actual shape of the failure: the same three pieces of work, reworded
    on each pass, must stay three items."""
    state = [{"content": MTCORE_VARIANTS[0], "status": "completed"}]
    for variant in MTCORE_VARIANTS[1:] * 4:
        state = merge_todos(state, [{"content": variant, "status": "completed"}])
    _, total = counts(state)
    assert total == 1, [t["content"] for t in state]


def test_the_same_file_named_two_very_different_ways_still_matches():
    """The path is the strongest signal these are one task."""
    a = [{"content": "Write src/core/mtBacktester.js plus derivsFeed.fundingRows and verify a real run",
          "status": "completed"}]
    b = [{"content": "src/core/mtBacktester.js — job wrapper, funding honesty, per-engine breakdown",
          "status": "pending"}]
    assert len(merge_todos(a, b)) == 1


def test_numbered_steps_stay_separate():
    """"item 0" and "item 1" are 83% alike by character ratio. Collapsing a
    numbered plan into one entry would be a worse bug than the one being
    fixed."""
    previous = [{"content": "item 0", "status": "completed"}]
    incoming = [{"content": "item 1", "status": "pending"}]
    assert len(merge_todos(previous, incoming)) == 2


def test_engine_one_and_engine_two_are_two_pieces_of_work():
    """Long enough for fuzzy matching, and differing only in a digit."""
    a = "Implement engine 1 of the regime router with its detector and sizing rules"
    b = "Implement engine 2 of the regime router with its detector and sizing rules"
    previous = [{"content": a, "status": "completed"}]
    assert len(merge_todos(previous, [{"content": b, "status": "pending"}])) == 2


def test_genuinely_different_work_is_never_merged():
    previous = [{"content": "Add MT_* defaults to src/strategies/defaultSettings.js (44 keys)",
                 "status": "completed"}]
    incoming = [{"content": "Extend chartOverlays.ts with drawMtLines and wire into TradingChart.tsx",
                 "status": "pending"}]
    assert len(merge_todos(previous, incoming)) == 2


def test_completed_status_still_survives_a_rewording():
    """The original point of merge_todos: finishing something and having the
    rewrite call it pending must not un-finish it."""
    previous = [{"content": MTCORE_VARIANTS[0], "status": "completed"}]
    incoming = [{"content": MTCORE_VARIANTS[2], "status": "pending"}]
    merged = merge_todos(previous, incoming)
    assert len(merged) == 1
    assert merged[0]["status"] == "completed"
