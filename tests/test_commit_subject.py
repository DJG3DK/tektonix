"""The final commit's subject and body come from the goal, whatever its shape."""
from agent import commit_subject as cs

PLAN = """# Plan: Give the backtest optimizer a proper curated, grouped sweep catalog

## Problem (confirmed in code)

The optimizer's settings dropdown is driven by one registry and the single-run
form by another. That is why one is grouped and the other is not.

## Steps

1. Build the catalog.
2. Serve it.
""" + "\n".join(f"- detail {i}" for i in range(40))


def test_a_pasted_plan_gets_a_one_line_subject_without_the_heading_markers():
    assert cs.subject(PLAN) == "Give the backtest optimizer a proper curated, grouped sweep catalog"


def test_a_long_goal_contributes_only_its_first_paragraph():
    body = cs.body(PLAN)
    assert body.startswith("The optimizer's settings dropdown")
    assert "detail 39" not in body
    assert body.endswith("The full task text is in the task record.")


def test_a_one_line_goal_is_the_whole_message():
    assert cs.message("Fix the login redirect loop") == "Fix the login redirect loop"


def test_a_short_multi_line_goal_is_quoted_whole():
    goal = "Fix the login redirect loop\n\nIt loops when the session cookie is stale."
    assert cs.message(goal) == goal


def test_a_long_line_is_cut_at_a_word():
    goal = "Rework the " + "very " * 30 + "long feature"
    s = cs.subject(goal)
    assert len(s) <= cs.SUBJECT_MAX + 3 and s.endswith("...") and not s.endswith(" ...")


def test_labels_and_emphasis_are_stripped():
    assert cs.subject("**Task:** tidy the settings page") == "tidy the settings page"


def test_an_empty_goal_still_has_a_subject():
    assert cs.subject("   \n\n") == "Task change"
    assert cs.message("") == "Task change"
