"""Keeping a task alive when main moves under it.

The merge into live is --ff-only, which is what guarantees the thing that
merges is the thing that was reviewed. The cost is that a branch whose base
moved cannot land at all: a reviewed, approved commit with nowhere to go, at
the end of a task somebody already paid for. The branch moves instead of the
rule.

Real repositories, real rebases. Mocking git here would test the mock.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from agent.tools.git import rebase_onto_base


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo on `main` with one commit, plus a task branch off it."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "app.py").write_text("one\ntwo\nthree\n")
    (r / "other.py").write_text("untouched\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-qb", "agent/task")
    return r


def _commit_on(repo, branch, path, text, msg):
    _git(repo, "checkout", "-q", branch)
    (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def test_a_base_that_has_not_moved_costs_nothing(repo):
    _commit_on(repo, "agent/task", "app.py", "one\ntwo\nCHANGED\n", "task work")
    before = _git(repo, "rev-parse", "HEAD")

    r = asyncio.run(rebase_onto_base(str(repo)))

    assert r["ok"] and r["moved"] is False
    assert _git(repo, "rev-parse", "HEAD") == before, "nothing was rewritten"


def test_a_moved_base_is_rebased_onto(repo):
    _commit_on(repo, "agent/task", "app.py", "one\ntwo\nCHANGED\n", "task work")
    _commit_on(repo, "main", "other.py", "main moved on\n", "someone else pushed")
    _git(repo, "checkout", "-q", "agent/task")
    main_tip = _git(repo, "rev-parse", "main")

    r = asyncio.run(rebase_onto_base(str(repo)))

    assert r["ok"] and r["moved"] and r["rebased"]
    assert _git(repo, "merge-base", "HEAD", "main") == main_tip, "now forks from the new tip"
    assert (repo / "other.py").read_text() == "main moved on\n", "carries main's work"
    assert (repo / "app.py").read_text() == "one\ntwo\nCHANGED\n", "and keeps its own"


def test_an_untouched_patch_does_not_need_reviewing_again(repo):
    """A rebase rewrites every sha, and the reviewer discards history when the
    sha it reviewed is no longer an ancestor -- so without this, every push by
    somebody else costs a full review round that learns nothing."""
    _commit_on(repo, "agent/task", "app.py", "one\ntwo\nCHANGED\n", "task work")
    _commit_on(repo, "main", "other.py", "main moved on\n", "unrelated push")
    _git(repo, "checkout", "-q", "agent/task")

    r = asyncio.run(rebase_onto_base(str(repo)))

    assert r["patch_identical"] is True


def test_a_patch_the_rebase_changed_is_reported_as_changed(repo):
    """Main editing the same file shifts this branch's context lines. The diff
    is no longer the one that was reviewed, so say so: re-reviewing is the
    conservative answer and this is the flag that asks for it."""
    _commit_on(repo, "agent/task", "app.py", "one\ntwo\nCHANGED\n", "task work")
    _commit_on(repo, "main", "app.py", "ZERO\none\ntwo\nthree\n", "main edits the same file")
    _git(repo, "checkout", "-q", "agent/task")

    r = asyncio.run(rebase_onto_base(str(repo)))

    assert r["ok"] and r["rebased"]
    assert r["patch_identical"] is False


def test_a_conflict_leaves_the_branch_exactly_as_it_was(repo):
    """The rebase is aborted before returning. A half-rebased worktree is the
    one state nobody can act on -- not the agent, not the reviewer, not a
    person."""
    _commit_on(repo, "agent/task", "app.py", "one\ntwo\nMINE\n", "task work")
    _commit_on(repo, "main", "app.py", "one\ntwo\nTHEIRS\n", "main touches the same line")
    _git(repo, "checkout", "-q", "agent/task")
    before = _git(repo, "rev-parse", "HEAD")

    r = asyncio.run(rebase_onto_base(str(repo)))

    assert r["ok"] is False and r["moved"] and r["rebased"] is False
    assert "app.py" in r["conflicts"], "names the file somebody has to fix"
    assert _git(repo, "rev-parse", "HEAD") == before, "branch is untouched"
    assert _git(repo, "status", "--porcelain") == "", "and the tree is clean"
    assert not (repo / ".git" / "rebase-merge").exists(), "no rebase left in progress"


def test_a_base_ref_that_does_not_exist_is_not_a_task_failure(repo):
    _commit_on(repo, "agent/task", "app.py", "one\ntwo\nCHANGED\n", "task work")
    r = asyncio.run(rebase_onto_base(str(repo), base_ref="no-such-branch"))
    assert r["ok"] is True and r["moved"] is False


def test_several_commits_of_main_are_all_carried(repo):
    _commit_on(repo, "agent/task", "app.py", "one\ntwo\nCHANGED\n", "task work")
    for i in range(3):
        _commit_on(repo, "main", f"m{i}.py", f"file {i}\n", f"push {i}")
    _git(repo, "checkout", "-q", "agent/task")

    r = asyncio.run(rebase_onto_base(str(repo)))

    assert r["ok"] and r["rebased"]
    for i in range(3):
        assert (repo / f"m{i}.py").exists(), f"m{i}.py from main survived"


# --- the merge endpoint's half ---------------------------------------------

def test_the_merge_endpoint_names_divergence_rather_than_failing_blind(tmp_path):
    """The server refuses a merge it cannot fast-forward. It used to do that by
    throwing, which reached the agent as a generic failure and stopped the task
    holding a reviewed, approved commit. It answers `diverged` now, which is
    what tells the caller to rebase and come back.

    This checks the condition the endpoint tests -- live ahead of the branch --
    against a real repository, without standing up the service.
    """
    live = tmp_path / "live"
    live.mkdir()
    _git(live, "init", "-q", "-b", "main")
    _git(live, "config", "user.email", "t@example.com")
    _git(live, "config", "user.name", "t")
    (live / "a.txt").write_text("base\n")
    _git(live, "add", "-A")
    _git(live, "commit", "-qm", "base")

    _git(live, "checkout", "-qb", "agent/task")
    (live / "b.txt").write_text("branch work\n")
    _git(live, "add", "-A")
    _git(live, "commit", "-qm", "task")

    _git(live, "checkout", "-q", "main")
    ahead = _git(live, "rev-list", "--count", "agent/task..HEAD")
    assert ahead == "0", "nothing has diverged yet, so a fast-forward is possible"

    (live / "c.txt").write_text("somebody else\n")
    _git(live, "add", "-A")
    _git(live, "commit", "-qm", "concurrent push")

    ahead = _git(live, "rev-list", "--count", "agent/task..HEAD")
    assert int(ahead) == 1, "live is ahead, so --ff-only would refuse"

    # And the recovery the endpoint asks for actually works.
    _git(live, "checkout", "-q", "agent/task")
    r = asyncio.run(rebase_onto_base(str(live)))
    assert r["ok"] and r["rebased"]
    _git(live, "checkout", "-q", "main")
    assert _git(live, "rev-list", "--count", "agent/task..HEAD") == "0", "now it can fast-forward"
