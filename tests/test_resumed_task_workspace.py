"""A resumed task gets its own branch back, on the current base.

2026-09-23: a task resumed after seven other merges woke on a detached, stale
main -- the first-pass sync never runs on a resume -- hand-copied main's files
over it because its git dir is read-only, and then the approved-commit fast
path tried to ship its OLD commit past the new work and died on "cannot
rebase: You have unstaged changes". These pin the restore, and the two guards
that stop an old approval shipping over new work.
"""
import subprocess

import pytest

from agent.tools import git as g

pytestmark = pytest.mark.asyncio

TASK = "11111111-2222-3333-4444-555555555555"
BRANCH = f"agent/{TASK}"


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null",
                 "GIT_CONFIG_SYSTEM": "/dev/null"}.items():
        monkeypatch.setenv(k, v)


def _run(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _identify(repo):
    """In the repo, not the environment: the agent's git runner does not pass
    GIT_* variables through, so a rebase it runs sees only repo config -- and
    CI has no global identity to fall back on."""
    _run(["config", "user.email", "t@x"], repo)
    _run(["config", "user.name", "t"], repo)


def _commit(repo, files, msg):
    for name, body in files.items():
        (repo / name).write_text(body)
    _run(["add", "-A"], repo)
    _run(["commit", "-q", "-m", msg], repo)


@pytest.fixture
def repo(tmp_path):
    """live on main, a worktree, and this task's branch committed off main."""
    live = tmp_path / "live"
    live.mkdir()
    _run(["init", "-q", "-b", "main"], live)
    _identify(live)
    _commit(live, {"app.py": "v1\n", "other.py": "o1\n"}, "base")
    ws = tmp_path / "ws"
    _run(["worktree", "add", "-q", str(ws), "-b", BRANCH], live)
    _commit(ws, {"app.py": "task change\n"}, "task")
    return live, ws


def _head(ws):
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], ws).stdout.strip()


async def test_another_tasks_checkout_is_replaced_by_this_tasks_branch(repo):
    live, ws = repo
    _run(["checkout", "-q", "--detach", "main"], ws)          # where the last task left it
    out = await g.restore_task_workspace(str(ws), TASK)
    assert out["restored"] is True
    assert _head(ws) == BRANCH
    assert (ws / "app.py").read_text() == "task change\n"


async def test_the_branch_is_rebased_when_main_moved_without_conflict(repo):
    live, ws = repo
    _run(["checkout", "-q", "--detach", "main"], ws)
    _commit(live, {"other.py": "o2\n"}, "someone else's merge")
    out = await g.restore_task_workspace(str(ws), TASK)
    assert out["rebase"]["rebased"] is True
    assert (ws / "other.py").read_text() == "o2\n"             # main's change
    assert (ws / "app.py").read_text() == "task change\n"     # and this task's


async def test_a_conflicting_branch_is_reset_onto_main_and_the_old_commit_kept(repo):
    live, ws = repo
    old = _run(["rev-parse", BRANCH], live).stdout.strip()
    _run(["checkout", "-q", "--detach", "main"], ws)
    _commit(live, {"app.py": "main's version\n"}, "conflicting merge")
    out = await g.restore_task_workspace(str(ws), TASK)
    assert out["reset_onto_base"] is True
    assert out["conflicts"] == ["app.py"]
    assert _head(ws) == BRANCH
    assert (ws / "app.py").read_text() == "main's version\n"  # agent re-applies on this
    assert _run(["rev-parse", out["backup"]], live).stdout.strip() == old


async def test_someone_elses_dirty_tree_is_stashed_not_inherited(repo):
    live, ws = repo
    _run(["checkout", "-q", "--detach", "main"], ws)
    (ws / "other.py").write_text("debris\n")
    out = await g.restore_task_workspace(str(ws), TASK)
    assert out["salvaged_from"]
    assert _head(ws) == BRANCH
    assert "debris" in _run(["stash", "show", "-p"], ws).stdout


async def test_its_own_uncommitted_work_on_its_branch_is_left_alone(repo):
    live, ws = repo
    await g._claim_workspace(str(ws), TASK)
    (ws / "app.py").write_text("more work in progress\n")
    out = await g.restore_task_workspace(str(ws), TASK)
    assert out["restored"] is False
    assert (ws / "app.py").read_text() == "more work in progress\n"


async def test_a_task_that_never_committed_syncs_like_a_fresh_one(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    _run(["init", "-q", "-b", "main"], live)
    _identify(live)
    _commit(live, {"app.py": "v1\n"}, "one")
    ws = tmp_path / "ws"
    _run(["worktree", "add", "-q", "--detach", str(ws), "HEAD"], live)
    _commit(live, {"app.py": "v2\n"}, "two")
    out = await g.restore_task_workspace(str(ws), TASK)
    assert out.get("synced") is True
    assert (ws / "app.py").read_text() == "v2\n"


# --- an old approval never ships over new work -------------------------------

async def test_the_fast_path_requires_a_clean_tree():
    from pathlib import Path
    src = Path("agent/nodes/verify_and_ship.py").read_text()
    fast = src[src.index('approved = state.get("merge_approved_sha")'):][:1200]
    assert 'status --porcelain' in fast


async def test_resuming_with_a_message_voids_the_old_approval(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import agent.server as server
    from agent.routers import tasks as tasks_routes

    # The route reads the app off the request (agent/routers/tasks.py).
    request = SimpleNamespace(app=server.app)

    class _Ck:
        values = {"repo": "p", "goal": "g", "budget_usd": 5.0, "max_iterations": 40,
                  "escalated": True, "escalation_reason": "x",
                  "committed_sha": "abc", "merge_approved_sha": "abc"}

    class _G:
        patches = []
        async def aget_state(self, cfg): return _Ck()
        async def aupdate_state(self, cfg, patch, **kw): self.patches.append(dict(patch))

    class _S:
        async def aget(self, ns, key): return None

    g_ = _G()
    monkeypatch.setattr(server.app.state, "graph", g_, raising=False)
    monkeypatch.setattr(server.app.state, "store", _S(), raising=False)
    monkeypatch.setattr(tasks_routes, "check_repo_access", lambda *a, **k: None)

    async def _stream(*a, **k): pass
    monkeypatch.setattr(server.app.state, "stream_graph", _stream, raising=False)
    await server.resume_task(request, "t-2", server.ResumeTaskRequest(additional_budget_usd=0, message="redo it"), user=object())
    await asyncio.sleep(0)
    server._running_tasks.pop("t-2", None)
    assert g_.patches[-1]["merge_approved_sha"] is None
    assert g_.patches[-1]["pending_feedback"]
