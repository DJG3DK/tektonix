"""Every phase x action of the task lifecycle, and the invariants between them.

Each invariant below was broken live on 2026-09-23, in a jump between states
rather than in any agent's work. This walks the whole table, so a new action,
a new phase, or a patch that forgets one field fails here instead.
"""
import itertools

import pytest
from langgraph.graph import END

from agent import lifecycle as lc
from agent.outer_graph import _route_after_verify

COMMIT = "c0ffee1"


def _values(phase: str, **over) -> tuple[dict, str | None]:
    """A representative checkpoint for each phase, and the store's status."""
    v = {"repo": "p", "goal": "g", "budget_usd": 5.0, "max_iterations": 40,
         "escalated": False, "escalation_reason": None, "pending_approval": None,
         "pending_merge_approval": None, "merge_approved_sha": None,
         "committed_sha": COMMIT, "pending_feedback": None, "approval_decision": None,
         "no_diff_streak": 2, "stale_pending_review_streak": 3}
    if phase == "escalated":
        v.update(escalated=True, escalation_reason="review service did not review x within 900s")
    elif phase == "awaiting_approval":
        v.update(pending_approval={"action_requests": [{"name": "bash", "args": {"command": "rm x"}}]})
    elif phase == "awaiting_merge":
        v.update(pending_merge_approval={"sha": COMMIT})
    v.update(over)
    status = {"escalated": "escalated", "awaiting_approval": "awaiting_approval",
              "awaiting_merge": "awaiting_merge"}.get(phase, phase)
    return v, status


def _call(action: str, values: dict, status: str | None, variant: str = "") -> lc.Transition:
    if action == "resume":
        msg = "please also do x" if variant == "msg" or status == "done" else None
        return lc.resume(values, status, message=msg, additional_budget=0.0)
    if action == "merge_approve":
        return lc.merge_decision(values, status, "approve", None)
    if action == "merge_request_changes":
        return lc.merge_decision(values, status, "request_changes", "rename it")
    if action == "command_decision":
        return lc.command_decision(values, status, "approve", None)
    if action == "operator_edit":
        return lc.operator_edit(values, status, base_sha=COMMIT, files=[{"path": "a.js", "content": "x"}],
                                note=None, by="op@x")
    if action == "heal":
        return lc.heal(values, status, reason="review_timeout", attempt=1, stage=variant or "gate")
    if action == "conclude_landed":
        return lc.conclude_landed(values, status)
    raise AssertionError(action)


def _merged(values: dict, t: lc.Transition) -> dict:
    return {**values, **t.patch}


def _next(values: dict, t: lc.Transition) -> str | None:
    """Where the graph goes after this transition. None: the checkpoint's own
    next node (a run that simply carries on)."""
    if t.as_node == "work":
        return "verify_and_ship"       # outer_graph: work -> verify_and_ship, unconditionally
    if t.as_node == "verify_and_ship":
        return _route_after_verify(_merged(values, t))
    return None


# ── the table itself ─────────────────────────────────────────────────────────

def test_every_action_has_a_row():
    assert set(lc.ALLOWED) == set(lc.ACTIONS)
    for phases in lc.ALLOWED.values():
        assert phases <= set(lc.PHASES)


@pytest.mark.parametrize("phase,action", list(itertools.product(lc.PHASES, lc.ACTIONS)))
def test_each_cell_is_allowed_or_refused_exactly_as_the_table_says(phase, action):
    values, status = _values(phase)
    if phase in lc.ALLOWED[action]:
        assert isinstance(_call(action, values, status), lc.Transition)
    else:
        with pytest.raises(lc.Refused) as e:
            _call(action, values, status)
        assert e.value.status == 409


# ── where each allowed transition goes ───────────────────────────────────────

WHERE = [
    # (phase, action, variant, values override, expected next node)
    ("escalated", "resume", "", {"merge_approved_sha": COMMIT}, "verify_and_ship"),  # re-ship, no model call
    ("escalated", "resume", "", {}, "work"),
    ("escalated", "resume", "msg", {"merge_approved_sha": COMMIT}, "work"),           # a note means more work
    ("done", "resume", "msg", {}, "work"),
    ("stopped", "resume", "", {}, None),
    ("error", "resume", "", {}, None),
    ("running", "resume", "", {}, None),
    ("awaiting_merge", "merge_approve", "", {}, "verify_and_ship"),
    ("awaiting_merge", "merge_request_changes", "", {}, "work"),
    ("awaiting_approval", "command_decision", "", {}, "work"),
    ("awaiting_merge", "operator_edit", "", {}, "verify_and_ship"),
    ("escalated", "heal", "gate", {}, "verify_and_ship"),
    ("escalated", "heal", "gate", {"merge_approved_sha": COMMIT}, "verify_and_ship"),
    ("escalated", "heal", "work", {}, "work"),
    ("escalated", "conclude_landed", "", {}, END),
    ("awaiting_merge", "conclude_landed", "", {}, END),
]


@pytest.mark.parametrize("phase,action,variant,over,expected", WHERE)
def test_each_transition_goes_where_it_should(phase, action, variant, over, expected):
    values, status = _values(phase, **over)
    assert _next(values, _call(action, values, status, variant)) == expected


def test_the_where_table_covers_every_allowed_cell():
    covered = {(p, a) for p, a, *_ in WHERE}
    allowed = {(p, a) for a, phases in lc.ALLOWED.items() for p in phases}
    assert allowed <= covered, f"no routing expectation for {sorted(allowed - covered)}"


# ── invariants, over every transition in the table ──────────────────────────

def _all_transitions():
    for phase, action, variant, over, _ in WHERE:
        values, status = _values(phase, **over)
        t = _call(action, values, status, variant)
        yield f"{phase}/{action}/{variant}/{sorted(over)}", values, t


TRANSITIONS = list(_all_transitions())


@pytest.mark.parametrize("name,values,t", TRANSITIONS, ids=[n for n, *_ in TRANSITIONS])
def test_the_agent_is_never_sent_to_work_without_an_instruction(name, values, t):
    if _next(values, t) == "work":
        m = _merged(values, t)
        assert m.get("pending_feedback") or m.get("approval_decision")


@pytest.mark.parametrize("name,values,t", TRANSITIONS, ids=[n for n, *_ in TRANSITIONS])
def test_an_old_approval_never_survives_new_work(name, values, t):
    """2026-09-23: a resume with instructions kept the approval, and the fast
    path shipped the OLD commit past the new work."""
    m = _merged(values, t)
    new_work = _next(values, t) == "work" and not m.get("approval_decision")
    if new_work or m.get("operator_edits"):
        assert m.get("merge_approved_sha") is None


@pytest.mark.parametrize("name,values,t", TRANSITIONS, ids=[n for n, *_ in TRANSITIONS])
def test_leaving_escalated_clears_it(name, values, t):
    if values.get("escalated") and _next(values, t) is not None:
        m = _merged(values, t)
        assert m["escalated"] is False and m["escalation_reason"] is None


@pytest.mark.parametrize("name,values,t", TRANSITIONS, ids=[n for n, *_ in TRANSITIONS])
def test_a_fresh_attempt_starts_its_streaks_over(name, values, t):
    """A streak carried across a decision gives the new attempt no runway."""
    if _next(values, t) == "work" and not _merged(values, t).get("approval_decision"):
        assert _merged(values, t)["no_diff_streak"] == 0


def test_a_merge_approval_approves_exactly_the_commit_shown():
    values, status = _values("awaiting_merge")
    m = _merged(values, _call("merge_approve", values, status))
    assert m["merge_approved_sha"] == values["pending_merge_approval"]["sha"] == m["committed_sha"]


def test_a_gate_heal_never_calls_the_model():
    values, status = _values("escalated")
    t = _call("heal", values, status, "gate")
    assert t.as_node == "work" and "pending_feedback" not in t.patch


def test_an_edit_against_another_commit_is_refused():
    values, status = _values("awaiting_merge")
    with pytest.raises(lc.Refused) as e:
        lc.operator_edit(values, status, base_sha="deadbee", files=[], note=None, by="op")
    assert e.value.status == 409


def test_a_done_task_needs_a_note_to_reopen():
    values, status = _values("done")
    with pytest.raises(lc.Refused) as e:
        lc.resume(values, status, message=None, additional_budget=0.0)
    assert e.value.status == 400


@pytest.mark.parametrize("phase", ["escalated", "awaiting_approval", "awaiting_merge"])
def test_every_resting_state_rests(phase):
    values, _ = _values(phase)
    assert _route_after_verify(values) == END
