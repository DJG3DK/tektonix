"""The agent's answer to a review round reaches the reviewer, and a benchmark
does not end as "escalated" over a review it has answered.

2026-09-25: a reviewer that reads only the diff repeated one false finding
three rounds running; the agent disproved it every round and nothing it
said ever reached the reviewer."""
import pathlib
import re

import agent.nodes.verify_and_ship as vs
from tests.test_verify_and_ship import (  # noqa: F401 -- the two fixtures are autouse there and needed here
    _fake_checks, _fake_return, _fake_review, _state, _stub_task_branch, _work_log_entry, autodetect_calls,
)

REVIEWER_JS = pathlib.Path("services/commit-reviewer/reviewer.js").read_text()


def _stub_commit_path(monkeypatch, messages: list, verdict="READY"):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))

    async def _commit(repo_root, message):
        messages.append(message)
        return {"ok": True}

    monkeypatch.setattr(vs, "git_commit", _commit)
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict=verdict))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": True}))


async def test_a_follow_up_commit_carries_the_agent_s_answer_to_the_round(monkeypatch):
    messages: list = []
    _stub_commit_path(monkeypatch, messages)
    state = _state(
        require_merge_review=False,
        review_gate_result={"verdict": "NEEDS_FIXES", "consecutiveNeedsFixes": 2, "lastReviewedSha": "old"},
        execution_log=[_work_log_entry("The finding is wrong: I ran pytest tests/test_x.py -k quote and it passes; "
                                       "the removed line is inside the CONTINUE branch.")],
    )
    await vs._verify_and_ship(state, config=None)
    assert len(messages) == 1
    assert f"{vs.REVIEW_RESPONSE_MARKER} 2:\n" in messages[0]
    assert "I ran pytest tests/test_x.py -k quote" in messages[0]


async def test_a_first_commit_carries_no_answer_section(monkeypatch):
    messages: list = []
    _stub_commit_path(monkeypatch, messages)
    state = _state(require_merge_review=False, execution_log=[_work_log_entry("done, all checks pass")])
    await vs._verify_and_ship(state, config=None)
    assert vs.REVIEW_RESPONSE_MARKER not in messages[0]


async def test_the_rejection_tells_the_agent_its_final_message_is_read(monkeypatch):
    messages: list = []
    _stub_commit_path(monkeypatch, messages, verdict="NEEDS_FIXES")
    result = await vs._verify_and_ship(_state(), config=None)
    assert "shown to the reviewer next round" in result["pending_feedback"]
    assert "escalated" not in result


async def test_a_benchmark_ships_a_disputed_fix_instead_of_escalating(monkeypatch):
    monkeypatch.setitem(vs.PROJECTS, "test-repo", {**(vs.PROJECTS.get("test-repo") or {}), "benchmark": True})
    messages: list = []
    _stub_commit_path(monkeypatch, messages)

    async def _escalated_review(*a, **k):
        return {"verdict": "NEEDS_FIXES", "summary": "same finding again", "escalated": True,
                "consecutiveNeedsFixes": 3, "findings": [{"severity": "blocking", "file": "x.py", "issue": "no"}]}

    monkeypatch.setattr(vs, "wait_for_review", _escalated_review)
    # verifier_runs=1: the gate's own verifier nudge comes first on a benchmark and is not what this tests
    result = await vs._verify_and_ship(_state(require_merge_review=False, verifier_runs=1), config=None)
    assert vs._is_terminal(result) and "escalated" not in result
    assert result["review_gate_result"]["disputed"] is True
    assert result["review_gate_result"]["verdict"] == "NEEDS_FIXES"
    assert any("disputed" in (e.get("summary") or "") for e in result["execution_log"])


async def test_a_live_project_still_hands_a_stuck_review_to_a_human(monkeypatch):
    messages: list = []
    _stub_commit_path(monkeypatch, messages)

    async def _escalated_review(*a, **k):
        return {"verdict": "NEEDS_FIXES", "summary": "same", "escalated": True, "findings": []}

    monkeypatch.setattr(vs, "wait_for_review", _escalated_review)
    result = await vs._verify_and_ship(_state(), config=None)
    assert result["escalated"] is True


def test_the_reviewer_reads_the_same_heading_the_gate_writes():
    m = re.search(r"const REVIEW_RESPONSE_MARKER = '([^']+)'", REVIEWER_JS)
    assert m and m.group(1) == vs.REVIEW_RESPONSE_MARKER


def test_the_reviewer_is_told_a_disproved_finding_is_withdrawn():
    assert "The agent's responses to the prior round(s)" in REVIEWER_JS
    assert "the finding is WITHDRAWN" in REVIEWER_JS
    assert "extractAgentResponses(fullCommitLog)" in REVIEWER_JS
