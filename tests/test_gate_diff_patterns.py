"""The ship gate sends a benchmark fix back once per diff pattern, before
the verifier nudge, and never on a live project."""
import agent.nodes.verify_and_ship as vs
from tests.test_diff_patterns import NEW_MESSAGE
from tests.test_verify_and_ship import (  # noqa: F401 -- autouse fixtures of that module
    _fake_checks, _fake_return, _fake_review, _state, _stub_task_branch, autodetect_calls,
)


def _stub(monkeypatch, diff):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(diff))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": True}))


async def test_a_new_error_message_is_sent_back_once_then_the_fix_proceeds(monkeypatch):
    monkeypatch.setitem(vs.PROJECTS, "test-repo", {**(vs.PROJECTS.get("test-repo") or {}), "benchmark": True})
    _stub(monkeypatch, NEW_MESSAGE)
    first = await vs._verify_and_ship(_state(verifier_runs=1, require_merge_review=False), config=None)
    assert first["pattern_nudges"] == ["new_error_text"]
    assert "Keep the existing message template" in first["pending_feedback"]
    assert "escalated" not in first
    second = await vs._verify_and_ship(
        _state(verifier_runs=1, require_merge_review=False, pattern_nudges=["new_error_text"]), config=None)
    assert vs._is_terminal(second) and "pending_feedback" not in second


async def test_a_live_project_is_not_gated_on_diff_patterns(monkeypatch):
    _stub(monkeypatch, NEW_MESSAGE)
    result = await vs._verify_and_ship(_state(require_merge_review=False), config=None)
    assert vs._is_terminal(result) and "pattern_nudges" not in result
