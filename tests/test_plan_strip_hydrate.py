"""GET /api/tasks/{id} hydrates the plan strip from the live todos mirror,
not from the checkpoint's end-of-pass copy (agent/server.py
_apply_plan_fallback). Reported 2026-09-09: a refresh mid-pass snapped a
3-item live plan back to the 15-item plan of the previous pass."""

from agent.server import _apply_plan_fallback, _todos_to_plan

OLD = [{"content": f"step {i}", "status": "completed" if i < 11 else "pending"} for i in range(15)]
LIVE = [{"content": "Diagnose and fix test:research-mcp failure", "status": "in_progress"},
        {"content": "Frontend settings/types", "status": "pending"},
        {"content": "Run checks", "status": "pending"}]


def test_live_mirror_beats_the_checkpoints_end_of_pass_plan():
    snapshot = {"plan": _todos_to_plan(OLD)}
    out = _apply_plan_fallback(snapshot, {"latest_todos": LIVE})
    assert len(out["plan"]) == 3
    assert out["plan"] == _todos_to_plan(LIVE)


def test_checkpoint_plan_is_used_when_there_is_no_mirror():
    snapshot = {"plan": _todos_to_plan(OLD)}
    assert _apply_plan_fallback(snapshot, {})["plan"] == _todos_to_plan(OLD)


def test_mirror_fills_in_when_the_checkpoint_has_no_plan_yet():
    out = _apply_plan_fallback({"plan": None}, {"latest_todos": LIVE})
    assert len(out["plan"]) == 3


def test_none_snapshot_stays_none():
    assert _apply_plan_fallback(None, {"latest_todos": LIVE}) is None
