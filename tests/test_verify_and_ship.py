"""Unit tests for verify_and_ship's branching logic -- the hard,
code-enforced gate. Covers the no-diff-streak terminal logic, the
escalated-guard short-circuit, and committed_sha tracking (a resume/retry
must never mistake "already committed, not yet reviewed" for "nothing to
ship").

All the real I/O functions (run_all_checks, git_diff, git_commit,
current_sha, trigger_check, wait_for_review, merge_and_deploy) are
monkeypatched at the point verify_and_ship.py imported them -- these tests
exercise the real branching/state logic, not real subprocesses or network
calls (those are covered by this session's own live end-to-end testing).
"""

import pytest

from agent.nodes import verify_and_ship as vs
from agent.outer_state import initial_state



@pytest.fixture(autouse=True)
def _stub_task_branch(monkeypatch):
    """Neutralise the per-task branch switch for every test in this module.

    The worktree migration (2ecc6c2) added `ensure_task_branch` to
    _verify_and_ship, but these tests were written before it existed and only
    stub `git_diff`/`git_commit`. So the real function ran against a tmpdir that
    is not a git repo, failed with "not a git repository", and the node
    escalated on THAT instead of reaching the behaviour under test -- 10 tests
    asserting on the wrong failure. Tests that care about branch handling
    override this with their own monkeypatch.
    """
    async def _ok(repo_root, task_id, base_ref="main"):
        return {"ok": True, "branch": f"agent/{task_id}", "output": ""}

    monkeypatch.setattr(vs, "ensure_task_branch", _ok)


@pytest.fixture(autouse=True)
def autodetect_calls(monkeypatch):
    """Record the post-merge check-detection hook instead of running it: the
    real one asks the reviewer over HTTP and, on this host, would find the
    live service. Tests that need an entry back set `entry` on the list."""
    class _Calls(list):
        entry = None

    calls = _Calls()

    async def _record(repo):
        calls.append(repo)
        return calls.entry

    monkeypatch.setattr(vs, "autodetect_checks_if_none", _record)
    return calls


def _state(**overrides):
    s = initial_state(task_id="t1", goal="do the thing", repo="test-repo", budget_usd=1.0)
    s.update(overrides)
    return s


def _config():
    """A real Config, because _write_episode's is not optional -- see its
    docstring."""
    from agent.config import load_config

    return load_config()


class FakeStore:
    """Minimal in-memory async store -- just enough for StoreBackend.awrite
    (aget then aput) to work, so _write_episode can be tested without a
    real Postgres connection.
    """

    def __init__(self):
        self.data: dict[tuple, dict] = {}

    async def aget(self, namespace, key):
        return self.data.get((namespace, key))

    async def aput(self, namespace, key, value):
        self.data[(namespace, key)] = type("Item", (), {"value": value})()


# ---------------------------------------------------------------------------
# escalated-guard / max_iterations
# ---------------------------------------------------------------------------


async def test_already_escalated_short_circuits_without_running_checks(monkeypatch):
    called = False

    async def fail_if_called(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(vs, "run_all_checks", fail_if_called)
    state = _state(escalated=True, escalation_reason="budget exhausted")

    result = await vs._verify_and_ship(state, config=None)

    assert result["escalated"] is True
    assert result["escalation_reason"] == "budget exhausted"
    assert called is False, "checks must not run on an already-escalated pass"


async def test_max_iterations_reached_escalates():
    state = _state(iteration_count=5, max_iterations=5)
    result = await vs._verify_and_ship(state, config=None)
    assert result["escalated"] is True
    assert "max_iterations" in result["escalation_reason"]


# ---------------------------------------------------------------------------
# checks fail
# ---------------------------------------------------------------------------


async def test_checks_fail_loops_back_and_resets_no_diff_streak(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=False, summary="lint failed"))
    state = _state(no_diff_streak=1)

    result = await vs._verify_and_ship(state, config=None)

    assert result["pending_feedback"] is not None
    assert "lint failed" in result["pending_feedback"]
    assert result["no_diff_streak"] == 0
    assert result["iteration_count"] == state["iteration_count"] + 1
    assert "escalated" not in result


# ---------------------------------------------------------------------------
# no diff (the streak logic)
# ---------------------------------------------------------------------------


async def test_no_diff_first_time_nudges_and_sets_streak_to_one(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    state = _state(no_diff_streak=0)

    result = await vs._verify_and_ship(state, config=None)

    assert result["no_diff_streak"] == 1
    assert result["pending_feedback"] is not None
    assert "escalated" not in result


async def test_no_diff_second_consecutive_time_terminates_done(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    state = _state(no_diff_streak=1)

    result = await vs._verify_and_ship(state, config=None)

    assert result["no_diff_streak"] == 0
    assert "escalated" not in result
    assert "pending_feedback" not in result
    assert vs._is_terminal(result) is True


def _work_log_entry(detail: str) -> dict:
    return {"node": "work", "step_id": None, "summary": "work pass complete", "detail": detail, "cost_usd": 0.0, "timestamp": "t"}


async def test_no_diff_with_short_cutoff_response_does_not_count_toward_done(monkeypatch):
    """A model can end a pass with finish_reason="stop" after a short burst
    of output, mid-sentence ("Let me look at the specific section... where
    the handler function is defined:") -- no tool call, no real conclusion.
    That must not be accepted as a legitimate no-diff pass, or the task ends
    "done" having never actually finished investigating. Even with the
    streak already at 1 (which would normally terminate), a short/cut-off-
    looking final response must reset the streak and loop back instead."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    state = _state(
        no_diff_streak=1,
        execution_log=[_work_log_entry("Let me look at the specific section of RequestHandler.tsx where the closeConnection function is defined:")],
    )

    result = await vs._verify_and_ship(state, config=None)

    assert result["no_diff_streak"] == 0
    assert "escalated" not in result
    assert vs._is_terminal(result) is False
    assert "cut off" in result["pending_feedback"]


async def test_no_diff_with_substantive_response_still_terminates_normally(monkeypatch):
    """A genuinely long, explicit no-changes-needed conclusion must NOT be
    penalized by the length check -- confirms the heuristic doesn't break
    the legitimate case it's designed to still allow."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    long_conclusion = (
        "The goal is already satisfied with no changes needed. The original request was explicitly "
        "read-only investigation, and after reviewing the relevant files I confirmed there is no "
        "code change required to satisfy it -- the behavior described is expected given the current "
        "design, not a bug."
    )
    state = _state(no_diff_streak=1, execution_log=[_work_log_entry(long_conclusion)])

    result = await vs._verify_and_ship(state, config=None)

    assert result["no_diff_streak"] == 0
    assert "escalated" not in result
    assert "pending_feedback" not in result
    assert vs._is_terminal(result) is True


async def test_no_diff_but_pending_commit_resumes_review_instead_of_nudging(monkeypatch):
    """The bug this whole committed_sha mechanism exists to prevent: a
    commit already made (pending review/deploy) must never be mistaken for
    "no changes needed" just because git diff is empty post-commit.
    """
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": True}))
    # no_diff_streak=1 would normally terminate as done_no_changes -- must
    # NOT, because a real commit is still outstanding.
    state = _state(require_merge_review=False, no_diff_streak=1, committed_sha="abc123")

    result = await vs._verify_and_ship(state, config=None)

    assert result.get("committed_sha") is None  # shipped successfully
    assert result["review_gate_result"]["verdict"] == "READY"
    assert "escalated" not in result


async def test_pending_commit_with_existing_nonready_verdict_nudges_instead_of_repolling(monkeypatch):
    """A model can stop acting on a NEEDS_FIXES verdict entirely (restating
    its plan in text, calling no tools, several passes in a row) while this
    branch keeps re-triggering the review service's real check suite on the
    unchanged commit every time -- guaranteed to get the identical verdict
    back, pure waste. If we already have a non-READY verdict for this exact
    sha, must nudge instead of calling wait_for_review/trigger_check again."""
    called = False

    async def fail_if_called(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    monkeypatch.setattr(vs, "current_sha", _fake_return("abc123"))  # HEAD == tracked sha (no self-commit)
    monkeypatch.setattr(vs, "trigger_check", fail_if_called)
    monkeypatch.setattr(vs, "wait_for_review", fail_if_called)
    state = _state(
        committed_sha="abc123",
        review_gate_result={"lastReviewedSha": "abc123", "verdict": "NEEDS_FIXES", "summary": "fix the thing"},
        stale_pending_review_streak=0,
    )

    result = await vs._verify_and_ship(state, config=None)

    assert called is False, "must not re-trigger a real review for an unchanged, already-rejected sha"
    assert result["stale_pending_review_streak"] == 1
    assert result["committed_sha"] == "abc123"
    assert "escalated" not in result
    assert "fix the thing" in result["pending_feedback"]
    assert vs._is_terminal(result) is False


async def test_stale_pending_review_restarts_thread_fresh_after_nudges_exhausted(monkeypatch):
    """The inner thread can degenerate into all-text-no-tool-calls history
    that every model call just pattern-matches -- nudges appended to that
    history can't fix it. Once nudges are exhausted, the first recovery step
    is a fresh inner thread (bump inner_thread_generation, seed distilled
    context via pending_feedback), not an escalation."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    monkeypatch.setattr(vs, "current_sha", _fake_return("abc123"))  # HEAD == tracked sha (no self-commit)
    state = _state(
        committed_sha="abc123",
        review_gate_result={
            "lastReviewedSha": "abc123", "verdict": "NEEDS_FIXES", "summary": "fix the thing",
            "findings": [{"severity": "blocking", "file": "a.ts", "issue": "the actual finding"}],
        },
        stale_pending_review_streak=vs.STALE_PENDING_REVIEW_LIMIT,
        inner_thread_generation=0,
    )

    result = await vs._verify_and_ship(state, config=None)

    assert "escalated" not in result
    assert result["inner_thread_generation"] == 1
    assert result["stale_pending_review_streak"] == 0  # fresh thread, fresh chances
    assert result["committed_sha"] == "abc123"
    # The fresh thread's seed context must carry everything a clean start needs:
    feedback = result["pending_feedback"]
    assert "do the thing" in feedback  # the goal
    assert "abc123"[:12] in feedback or "abc123" in feedback  # the pending commit to build on
    assert "the actual finding" in feedback  # the review findings
    assert vs._is_terminal(result) is False


async def test_stale_pending_review_escalates_only_after_a_fresh_thread_also_stalls(monkeypatch):
    """If the restarted thread stalls the exact same way, the problem isn't
    conversation shape -- NOW it's a genuine human handoff."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    monkeypatch.setattr(vs, "current_sha", _fake_return("abc123"))  # HEAD == tracked sha (no self-commit)
    state = _state(
        committed_sha="abc123",
        review_gate_result={"lastReviewedSha": "abc123", "verdict": "NEEDS_FIXES", "summary": "fix the thing"},
        stale_pending_review_streak=vs.STALE_PENDING_REVIEW_LIMIT,
        inner_thread_generation=vs.MAX_THREAD_RESTARTS,  # already used the restart
    )

    result = await vs._verify_and_ship(state, config=None)

    assert result["escalated"] is True
    assert "no progress" in result["escalation_reason"]
    assert "fresh-thread restart" in result["escalation_reason"]


async def test_a_fresh_review_verdict_resets_the_stale_streak(monkeypatch):
    """A GENUINE new review run (this sha hasn't been reviewed before, or
    the streak was already counting a DIFFERENT sha) must reset the streak
    to 0 -- it must not carry over and wrongly escalate a task that's
    actually making real progress."""
    triggered = False

    async def _wait_for_review(*a, **k):
        nonlocal triggered
        triggered = True
        return {"verdict": "NEEDS_FIXES", "summary": "reviewed", "findings": []}

    monkeypatch.setattr(vs, "sha_in_repo", _fake_return(False))  # unit test: never consult the real live repo
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _wait_for_review)
    # review_gate_result is for a DIFFERENT (older) sha -- this pass's
    # pending_sha has never actually been reviewed yet, so it must go
    # through _review_and_deploy normally, not the nudge branch.
    state = _state(
        committed_sha="def456",
        review_gate_result={"lastReviewedSha": "abc123", "verdict": "NEEDS_FIXES", "summary": "old finding"},
        stale_pending_review_streak=2,
    )

    result = await vs._verify_and_ship(state, config=None)

    assert triggered is True, "must actually run a real review for a sha it hasn't reviewed yet"
    assert result["stale_pending_review_streak"] == 0


# ---------------------------------------------------------------------------
# real diff -> commit -> review -> deploy
# ---------------------------------------------------------------------------


async def test_commit_fails_escalates(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": False, "output": "no git identity configured"}))
    state = _state()

    result = await vs._verify_and_ship(state, config=None)

    assert result["escalated"] is True
    assert "final commit failed" in result["escalation_reason"]


async def test_needs_fixes_loops_back_and_keeps_committed_sha(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(
        vs, "wait_for_review",
        _fake_review(verdict="NEEDS_FIXES", findings=[{"severity": "blocking", "file": "x.ts", "issue": "bug"}]),
    )
    state = _state()

    result = await vs._verify_and_ship(state, config=None)

    assert result["pending_feedback"] is not None
    assert "bug" in result["pending_feedback"]
    assert result["committed_sha"] == "deadbeef"
    assert result["no_diff_streak"] == 0
    assert "escalated" not in result


async def test_ready_and_deploy_succeeds_is_terminal_and_clears_committed_sha(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": True}))
    state = _state(require_merge_review=False, )

    result = await vs._verify_and_ship(state, config=None)

    assert vs._is_terminal(result) is True
    assert "escalated" not in result
    assert result["committed_sha"] is None
    assert result["review_gate_result"]["verdict"] == "READY"


async def test_deploy_fails_escalates_but_preserves_committed_sha(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": False, "stage": "restart"}))
    state = _state(require_merge_review=False, )

    result = await vs._verify_and_ship(state, config=None)

    assert result["escalated"] is True
    assert result["committed_sha"] == "deadbeef", (
        "a resume must know this sha is already reviewed+merged so it retries "
        "merge_and_deploy instead of re-committing or double-merging"
    )


# ---------------------------------------------------------------------------
# post-merge check detection (agent/project_checks.py)
# ---------------------------------------------------------------------------


def _ready_and_shipped(monkeypatch, deployed):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return(deployed))


async def test_successful_ship_runs_check_detection_and_logs_its_entry(monkeypatch, autodetect_calls):
    _ready_and_shipped(monkeypatch, {"ok": True})
    autodetect_calls.entry = {
        "node": "verify_and_ship", "step_id": None, "cost_usd": 0.0, "timestamp": "2026-09-16T00:00:00Z",
        "summary": "configured detected checks for test-repo: lint", "detail": "lint: npm run lint",
    }

    result = await vs._verify_and_ship(_state(require_merge_review=False), config=None)

    assert vs._is_terminal(result) is True
    assert autodetect_calls == ["test-repo"]
    summaries = [e["summary"] for e in result["execution_log"]]
    assert summaries[-2:] == ["merged and deployed", "configured detected checks for test-repo: lint"], \
        "the hook's entry rides along after the deploy entry"


async def test_successful_ship_with_nothing_to_detect_logs_nothing_extra(monkeypatch, autodetect_calls):
    _ready_and_shipped(monkeypatch, {"ok": True})
    result = await vs._verify_and_ship(_state(require_merge_review=False), config=None)
    assert autodetect_calls == ["test-repo"]
    assert [e["summary"] for e in result["execution_log"]][-1] == "merged and deployed"


async def test_failed_deploy_never_runs_check_detection(monkeypatch, autodetect_calls):
    _ready_and_shipped(monkeypatch, {"ok": False, "stage": "restart"})
    result = await vs._verify_and_ship(_state(require_merge_review=False), config=None)
    assert result["escalated"] is True
    assert autodetect_calls == [], "nothing shipped, so nothing to detect against"


async def test_failed_build_never_runs_check_detection(monkeypatch, autodetect_calls):
    _ready_and_shipped(monkeypatch, {"ok": False, "stage": "build", "error": "tsc: boom"})
    result = await vs._verify_and_ship(_state(require_merge_review=False), config=None)
    assert result["pending_feedback"] is not None
    assert autodetect_calls == []


async def test_deploy_build_failure_loops_back_instead_of_escalating(monkeypatch):
    """A build-time error (e.g. an unused-var typecheck failure left behind
    by an edit) is a real, agent-fixable code error -- not an infra problem --
    so it should loop back with the actual build error, same as a
    NEEDS_FIXES verdict, instead of escalating to a human for something the
    agent can just fix."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(
        vs,
        "merge_and_deploy",
        _fake_return({"ok": False, "stage": "build", "error": "TS6133: 'x' is declared but never read."}),
    )
    state = _state(require_merge_review=False, iteration_count=1)

    result = await vs._verify_and_ship(state, config=None)

    assert "escalated" not in result
    assert result["iteration_count"] == 2
    assert result["no_diff_streak"] == 0
    assert result["committed_sha"] == "deadbeef", "sha stays tracked -- the commit is real, just unbuildable yet"
    assert "TS6133" in result["pending_feedback"]
    assert vs._is_terminal(result) is False


async def test_deploy_merge_failure_still_escalates(monkeypatch):
    """A merge conflict isn't a code error the agent can fix by editing --
    stage != "build" must still escalate, unlike the build-failure case."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": False, "stage": "merge"}))
    state = _state(require_merge_review=False, )

    result = await vs._verify_and_ship(state, config=None)

    assert result["escalated"] is True
    assert result["committed_sha"] == "deadbeef"


async def test_review_timeout_escalates_and_preserves_committed_sha(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))

    async def timeout(*a, **k):
        raise TimeoutError("the review service did not review deadbeef within 900s")

    monkeypatch.setattr(vs, "wait_for_review", timeout)
    state = _state()

    result = await vs._verify_and_ship(state, config=None)

    assert result["escalated"] is True
    assert result["committed_sha"] == "deadbeef"


# ---------------------------------------------------------------------------
# unexpected-exception safety net
# ---------------------------------------------------------------------------


async def test_unexpected_exception_becomes_a_normal_escalation_not_a_crash(monkeypatch):
    async def boom(*a, **k):
        raise ConnectionError("review service unreachable")

    monkeypatch.setattr(vs, "run_all_checks", boom)
    state = _state()

    result = await vs._verify_and_ship(state, config=None)

    assert result["escalated"] is True
    assert "review service unreachable" in result["escalation_reason"]


# ---------------------------------------------------------------------------
# _write_episode outcome inference
# ---------------------------------------------------------------------------


async def test_write_episode_infers_escalated_outcome():
    store = FakeStore()
    state = _state()
    result = {"escalated": True, "escalation_reason": "boom"}

    await vs._write_episode(store, state, result, _config())

    record = await _only_episode(store, state["repo"])
    assert record["outcome"] == "escalated"
    assert record["escalation_reason"] == "boom"


async def test_write_episode_infers_done_no_changes_outcome():
    store = FakeStore()
    state = _state()
    result = {"no_diff_streak": 0}  # _done_no_changes()'s shape -- no review_gate_result, not escalated

    await vs._write_episode(store, state, result, _config())

    record = await _only_episode(store, state["repo"])
    assert record["outcome"] == "done_no_changes"


async def test_write_episode_infers_shipped_outcome():
    store = FakeStore()
    state = _state()
    result = {"review_gate_result": {"verdict": "READY"}}

    await vs._write_episode(store, state, result, _config())

    record = await _only_episode(store, state["repo"])
    assert record["outcome"] == "shipped"
    assert record["review_verdict"] == "READY"


# ---------------------------------------------------------------------------
# _is_terminal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"escalated": True}, True),
        ({"pending_feedback": "keep going"}, False),
        ({"pending_feedback": None}, True),
        ({}, True),
    ],
)
def test_is_terminal(result, expected):
    assert vs._is_terminal(result) is expected


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _fake_checks(all_ok: bool, summary: str = ""):
    async def f(*a, **k):
        return {"all_ok": all_ok, "summary": summary}

    return f


def _fake_return(value):
    async def f(*a, **k):
        return value

    return f


def _fake_review(verdict: str, findings: list | None = None):
    async def f(*a, **k):
        return {"verdict": verdict, "summary": "reviewed", "findings": findings or []}

    return f


async def _only_episode(store: FakeStore, repo: str) -> dict:
    import json

    from deepagents.backends import StoreBackend
    from deepagents.backends.utils import file_data_to_string

    from agent.deep_agent import episodes_namespace

    assert len(store.data) == 1, f"expected exactly one episode written, got {len(store.data)}"
    (path,) = (k[1] for k in store.data)
    backend = StoreBackend(namespace=episodes_namespace(repo), store=store)
    result = await backend.aread(path)
    return json.loads(file_data_to_string(result.file_data))


async def test_self_committed_head_is_adopted_instead_of_polling_stale_sha(monkeypatch):
    """A model can run `git commit` itself via bash, moving HEAD past the
    gate's tracked pending sha. The gate then has no diff plus a pending sha
    that would never get a (new) verdict -- wait_for_review would burn its
    full 900s timeout on the stale sha and escalate for nothing. The gate
    must adopt the real HEAD as the pending commit instead."""
    seen = {}

    async def _wait(repo, sha, timeout):
        seen["sha"] = sha
        return {"verdict": "NEEDS_FIXES", "summary": "reviewed head", "findings": []}

    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    monkeypatch.setattr(vs, "current_sha", _fake_return("selfhead999"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _wait)
    state = _state(committed_sha="stale123")

    result = await vs._verify_and_ship(state, config=None)

    assert seen["sha"] == "selfhead999", "must review the REAL head, not the stale tracked sha"
    assert result["committed_sha"] == "selfhead999"


async def test_no_diff_pending_sha_already_in_live_concludes_without_review(monkeypatch, tmp_path):
    """Auto-merge race: the review service can merge+deploy on READY and
    consume the verdict entry; the gate must recognize its pending commit is
    already in the live repo and conclude instead of nudging for a verdict
    that will never appear."""
    import os
    import subprocess

    live = tmp_path / "live"
    live.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=live, check=True, env=env)
    subprocess.run(["git", "-C", str(live), "commit", "--allow-empty", "-q", "-m", "shipped"],
                   check=True, env=env)
    sha = subprocess.run(["git", "-C", str(live), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()

    monkeypatch.setitem(vs.PROJECTS, "test-repo", {"sandbox": str(tmp_path), "live": str(live)})
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    monkeypatch.setattr(vs, "current_sha", _fake_return(sha))

    async def _boom(*a, **k):
        raise AssertionError("review/merge must not run when live already has the sha")
    monkeypatch.setattr(vs, "trigger_check", _boom)
    monkeypatch.setattr(vs, "wait_for_review", _boom)
    monkeypatch.setattr(vs, "merge_and_deploy", _boom)

    state = _state(no_diff_streak=0, committed_sha=sha)
    result = await vs._verify_and_ship(state, config=None)

    assert result.get("committed_sha") is None, "shipped -- nothing left to track"
    assert (result.get("review_gate_result") or {}).get("verdict") == "READY"
    assert any("auto-merge" in (e.get("summary") or "") for e in result.get("execution_log") or [])


async def test_auto_merged_conclusion_also_runs_check_detection(monkeypatch, tmp_path, autodetect_calls):
    """The auto-merge path is a ship too; a project whose every merge lands
    that way must still get its checks after the first one."""
    import os
    import subprocess

    live = tmp_path / "live"
    live.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=live, check=True, env=env)
    subprocess.run(["git", "-C", str(live), "commit", "--allow-empty", "-q", "-m", "shipped"],
                   check=True, env=env)
    sha = subprocess.run(["git", "-C", str(live), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    monkeypatch.setitem(vs.PROJECTS, "test-repo", {"sandbox": str(tmp_path), "live": str(live)})
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return(""))
    monkeypatch.setattr(vs, "current_sha", _fake_return(sha))
    autodetect_calls.entry = {
        "node": "verify_and_ship", "step_id": None, "cost_usd": 0.0, "timestamp": "2026-09-16T00:00:00Z",
        "summary": "configured detected checks for test-repo: test", "detail": "test: npm test",
    }

    result = await vs._verify_and_ship(_state(no_diff_streak=0, committed_sha=sha), config=None)

    assert autodetect_calls == ["test-repo"]
    assert [e["summary"] for e in result["execution_log"]][-1] == "configured detected checks for test-repo: test"


# ---------------------------------------------------------------------------
# don't commit a half-finished plan
# ---------------------------------------------------------------------------
#
# Confirmed live 2026-08-23: a task committed at step 5 of its own 8-step
# todo list. Checks passed on the part it had done, so the diff was committed
# and sent for review -- and the review service, with no way to know the
# commit was a mid-plan checkpoint, reported the not-yet-written pieces (a
# backfill script that was step 6, tests that were step 7) as blocking
# defects. The task was bounced on findings that described scheduled work.


def _todos(*pairs):
    return [{"content": c, "status": s} for c, s in pairs]


async def test_unfinished_plan_holds_the_commit_and_names_whats_left(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("must not commit while the plan is unfinished")

    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", fail_if_called)
    state = _state(latest_todos=_todos(
        ("Investigate the codebase", "completed"),
        ("Write the backfill script", "pending"),
        ("Delegate tests to test-writer", "in_progress"),
    ))

    result = await vs._verify_and_ship(state, config=None)

    assert result["incomplete_plan_streak"] == 1
    assert result["pending_feedback"] is not None
    # The two outstanding items are named; the finished one isn't dragged back up.
    assert "Write the backfill script" in result["pending_feedback"]
    assert "Delegate tests to test-writer" in result["pending_feedback"]
    assert "Investigate the codebase" not in result["pending_feedback"]


async def test_completed_plan_commits_normally(monkeypatch):
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": True}))
    state = _state(latest_todos=_todos(("Do the thing", "completed"), ("Test it", "completed")))

    result = await vs._verify_and_ship(state, config=None)

    assert result.get("escalated") is not True
    assert result["incomplete_plan_streak"] == 0


async def test_task_with_no_todo_list_is_unaffected(monkeypatch):
    """A task that never called write_todos has no plan to be judged
    against -- it must still commit exactly as it always did."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": True}))
    state = _state(latest_todos=None)

    result = await vs._verify_and_ship(state, config=None)

    assert result.get("escalated") is not True


async def test_nudge_budget_exhausted_commits_anyway(monkeypatch):
    """The todo list is the model's own self-report. A model that stops
    maintaining it must not be able to strand real, checks-passing work as
    an uncommittable diff forever -- past INCOMPLETE_PLAN_LIMIT the commit
    goes through regardless."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": True}))
    state = _state(
        latest_todos=_todos(("Never finished", "pending")),
        incomplete_plan_streak=vs.INCOMPLETE_PLAN_LIMIT,
    )

    result = await vs._verify_and_ship(state, config=None)

    assert result.get("escalated") is not True
    assert result["incomplete_plan_streak"] == 0  # budget starts over for the next plan


# ── the operator's final look (merge approval) ──────────────────────────────

async def test_ready_parks_on_merge_approval_when_required(monkeypatch):
    """Default behaviour: a READY verdict does NOT merge. The task parks with
    pending_merge_approval carrying the exact sha the operator will be shown,
    and merge_and_deploy is never called."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    merged = []
    async def _merge(repo):
        merged.append(repo)
        return {"ok": True}
    monkeypatch.setattr(vs, "merge_and_deploy", _merge)
    state = _state()  # require_merge_review defaults True

    result = await vs._verify_and_ship(state, config=None)

    assert merged == [], "merge must NOT run before the operator's decision"
    assert result["pending_merge_approval"]["sha"] == "deadbeef"
    assert result["committed_sha"] == "deadbeef", "the outstanding commit stays tracked for the resume"
    assert "escalated" not in result


async def test_approved_sha_merges_and_clears_approval_state(monkeypatch):
    """The re-entry pass after the operator approves: merge_approved_sha
    matches the outstanding commit, so the gate steps aside and the normal
    merge path runs -- and both approval fields are consumed by the merge, so
    a FUTURE commit can never inherit this approval."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    monkeypatch.setattr(vs, "merge_and_deploy", _fake_return({"ok": True}))
    state = _state(merge_approved_sha="deadbeef")

    result = await vs._verify_and_ship(state, config=None)

    assert result["committed_sha"] is None
    assert result["pending_merge_approval"] is None
    assert result["merge_approved_sha"] is None


async def test_approval_for_a_DIFFERENT_sha_does_not_merge(monkeypatch):
    """The race the sha equality exists for: the operator approved one commit,
    but a newer commit is now outstanding. The stale approval must not ship
    code the operator never saw -- the task parks again on the NEW sha."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("aaaa1111"))
    monkeypatch.setattr(vs, "trigger_check", _fake_return(None))
    monkeypatch.setattr(vs, "wait_for_review", _fake_review(verdict="READY"))
    merged = []
    async def _merge(repo):
        merged.append(repo)
        return {"ok": True}
    monkeypatch.setattr(vs, "merge_and_deploy", _merge)
    state = _state(merge_approved_sha="deadbeef")  # approved an OLDER commit

    result = await vs._verify_and_ship(state, config=None)

    assert merged == [], "a stale approval must never ship an unseen commit"
    assert result["pending_merge_approval"]["sha"] == "aaaa1111"


async def test_trigger_check_failure_preserves_committed_sha(monkeypatch):
    """audit C-6: a review-trigger failure right after commit must not strand
    the commit. The escalation must carry committed_sha so a resume re-polls
    review for it instead of concluding 'no changes needed'."""
    monkeypatch.setattr(vs, "run_all_checks", _fake_checks(all_ok=True))
    monkeypatch.setattr(vs, "git_diff", _fake_return("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", _fake_return({"ok": True}))
    monkeypatch.setattr(vs, "current_sha", _fake_return("deadbeefcafe"))

    async def _boom(repo):
        raise ConnectionRefusedError("review control port down")
    monkeypatch.setattr(vs, "trigger_check", _boom)
    state = _state(require_merge_review=False)

    result = await vs._verify_and_ship(state, config=None)

    assert result.get("escalated") is True
    assert result.get("committed_sha") == "deadbeefcafe", \
        "the freshly-committed sha must survive a trigger_check failure"


async def test_the_node_hands_its_config_to_the_episode_writer(monkeypatch):
    """The whole point of agent/episodes.py is that the writer has a config
    to work from -- the index hook and the embedding digest both need one.
    Nothing downstream reads it yet, so this is the only thing that would
    notice if the wire came loose.
    """
    seen = {}

    async def record(store, config, repo, record_dict):
        seen["config"] = config
        seen["repo"] = repo
        return "/memories/episodes/x.json"

    monkeypatch.setattr(vs, "write_episode", record)
    monkeypatch.setattr(vs, "_verify_and_ship", _fake_return({"review_gate_result": {"verdict": "READY"}}))

    app_config = _config()
    await vs.verify_and_ship_node(_state(), app_config, FakeStore())

    assert seen["config"] is app_config, "the node's app_config must reach write_episode"
    assert seen["repo"] == "test-repo"


# ---------------------------------------------------------------------------
# a task parked on the operator's MERGE decision has not shipped
# ---------------------------------------------------------------------------
#
# Live 2026-09-21..22, and the chain is worth recording because every link
# looked fine on its own:
#
#   a task reached READY and parked on pending_merge_approval
#     -> _is_terminal guarded pending_approval but not this, so an episode was
#        written saying outcome="shipped"
#     -> agent/consolidation.py learns from episodes, and read "shipped" as
#        work that LANDED, writing into project memory: "a root package.json
#        now exists"
#     -> the operator never merged, so for a day the memory asserted a file
#        that was not on main
#     -> a planning session loaded that memory, concluded there was nothing to
#        build, and a build task started with a $10 budget against a goal that
#        was an essay explaining as much.
#
# The episode is deferred, never lost: approving re-enters the node, the merge
# happens, and the terminal write runs then.

def _parked_on_merge() -> dict:
    return {
        "committed_sha": "abc123",
        "review_gate_result": {"verdict": "READY", "lastReviewedSha": "abc123"},
        "pending_merge_approval": {"sha": "abc123", "repo": "demo", "at": 0},
        "execution_log": [],
    }


def test_parking_on_a_merge_decision_is_not_a_terminal_outcome():
    assert vs._is_terminal(_parked_on_merge()) is False


def test_the_same_commit_is_terminal_once_the_merge_has_happened():
    """What the approve path returns: the parking key is gone, so the episode
    is written then -- with the outcome it actually had."""
    merged = _parked_on_merge()
    merged["pending_merge_approval"] = None
    merged["committed_sha"] = None
    assert vs._is_terminal(merged) is True


def test_a_rejected_merge_loops_back_rather_than_writing_an_episode():
    sent_back = _parked_on_merge()
    sent_back["pending_merge_approval"] = None
    sent_back["pending_feedback"] = "the operator asked for changes"
    assert vs._is_terminal(sent_back) is False


def test_an_escalation_is_still_terminal_even_while_parked():
    """Escalation wins: a task that gave up has a real outcome to record, and
    burying it behind a stale parking flag would lose it."""
    escalated = _parked_on_merge()
    escalated["escalated"] = True
    assert vs._is_terminal(escalated) is True
