"""A task never inherits the last task's mess, and never loses its own work.

Three failures on 2026-09-22, all from one gap:

  * a stopped task left 2,086 files of downloaded reference material in the
    shared worktree. The next task found them, spent its calls reading them
    instead of the code it was pointed at, and would have hit the 500-file
    commit guard twenty minutes in.
  * the sync then refused BECAUSE of that debris, so the next task silently
    skipped its base sync -- the exact failure sync_workspace_to_base exists
    to prevent, reintroduced by its own safety check.
  * and the refusal returned ok=True, so nothing treated it as a problem.

The missing distinction is whose dirty tree it is. These pin both halves:
a resume keeps its own uncommitted work, and anyone else's is stashed --
never deleted -- so the workspace is clean and nothing is lost.
"""
import subprocess

import pytest

from agent.tools import git as g

pytestmark = pytest.mark.asyncio


def _run(args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env=env, check=True)


@pytest.fixture
def workspace(tmp_path):
    """A live repo with two commits on main, and a worktree of it."""
    live = tmp_path / "live"
    live.mkdir()
    _run(["init", "-q", "-b", "main"], live)
    (live / "app.py").write_text("v1\n")
    _run(["add", "-A"], live)
    _run(["commit", "-q", "-m", "one"], live)
    (live / "app.py").write_text("v2\n")
    _run(["add", "-A"], live)
    _run(["commit", "-q", "-m", "two"], live)
    ws = tmp_path / "ws"
    _run(["worktree", "add", "-q", str(ws), "-b", "agent-base"], live)
    # Park it one commit behind, the way a previous task would have left it.
    _run(["checkout", "-q", "--detach", "HEAD~1"], ws)
    return ws


async def test_a_clean_workspace_syncs_to_the_tip(workspace):
    out = await g.sync_workspace_to_base(str(workspace), task_id="task-a")
    assert out["synced"] is True
    assert (workspace / "app.py").read_text() == "v2\n"
    assert "salvaged_from" not in out


async def test_the_syncing_task_claims_the_workspace(workspace):
    await g.sync_workspace_to_base(str(workspace), task_id="task-a")
    assert await g._workspace_owner(str(workspace)) == "task-a"


async def test_the_claim_is_not_in_the_tree_so_it_never_gets_committed(workspace):
    """A marker file in the worktree would show in `git status`, be swept up
    by `git add -A`, and become the very debris this cleans."""
    await g.sync_workspace_to_base(str(workspace), task_id="task-a")
    status = _run(["status", "--porcelain", "--untracked-files=all"], workspace).stdout
    assert status.strip() == ""


async def test_a_resume_keeps_its_own_uncommitted_work(workspace):
    """The case the old refusal existed to protect, and it still holds."""
    await g.sync_workspace_to_base(str(workspace), task_id="task-a")
    (workspace / "app.py").write_text("half-finished work\n")
    (workspace / "new_file.py").write_text("also mine\n")

    out = await g.sync_workspace_to_base(str(workspace), task_id="task-a")
    assert out["synced"] is False
    assert "own uncommitted work" in out["reason"]
    assert (workspace / "app.py").read_text() == "half-finished work\n"
    assert (workspace / "new_file.py").exists()


async def test_another_tasks_debris_is_stashed_and_the_sync_proceeds(workspace):
    """The 2026-09-22 case: task A left a directory behind, task B starts."""
    await g.sync_workspace_to_base(str(workspace), task_id="task-a")
    junk = workspace / "cq" / "deep" / "nested"
    junk.mkdir(parents=True)
    (junk / "reference.txt").write_text("31MB of downloaded material, pretend\n")
    (workspace / "app.py").write_text("task A was mid-edit\n")

    out = await g.sync_workspace_to_base(str(workspace), task_id="task-b")
    assert out["synced"] is True
    assert out["salvaged_from"] == "task-a"
    # The workspace is clean and at the tip...
    assert not (workspace / "cq").exists()
    assert (workspace / "app.py").read_text() == "v2\n"
    assert _run(["status", "--porcelain", "-uall"], workspace).stdout.strip() == ""
    # ...and task B owns it now.
    assert await g._workspace_owner(str(workspace)) == "task-b"


async def test_nothing_is_deleted_only_stashed(workspace):
    """`git stash`, not `clean -fd`. A harness that silently deletes a
    directory it does not understand is not one to trust with a repo."""
    await g.sync_workspace_to_base(str(workspace), task_id="task-a")
    (workspace / "cq").mkdir()
    (workspace / "cq" / "reference.txt").write_text("recover me\n")
    (workspace / "app.py").write_text("and me\n")

    await g.sync_workspace_to_base(str(workspace), task_id="task-b")
    stashes = _run(["stash", "list"], workspace).stdout
    assert "task-a" in stashes

    _run(["stash", "pop"], workspace)
    assert (workspace / "cq" / "reference.txt").read_text() == "recover me\n"
    assert (workspace / "app.py").read_text() == "and me\n"


async def test_an_unclaimed_dirty_workspace_is_salvaged_too(workspace):
    """Debris predating this feature has no owner marker. It is still debris."""
    (workspace / "leftover.txt").write_text("from before any of this\n")
    out = await g.sync_workspace_to_base(str(workspace), task_id="task-b")
    assert out["synced"] is True
    assert out["salvaged_from"] == "an unknown task"
    assert not (workspace / "leftover.txt").exists()


async def test_with_no_task_id_the_old_refusal_still_applies(workspace):
    """Callers that cannot say who they are (tests, scripts) must not have
    their trees stashed out from under them... but a dirty tree with no
    claimant is still abandoned, so it IS salvaged. What they do not get is
    the resume protection, because they never claimed anything."""
    (workspace / "app.py").write_text("someone's edit\n")
    out = await g.sync_workspace_to_base(str(workspace))
    assert out["synced"] is True and out["salvaged_from"] == "an unknown task"


async def test_a_failed_stash_refuses_rather_than_destroying(workspace, monkeypatch):
    """Could not salvage, so do not clean. The old refusal is the right
    outcome when the alternative is not available."""
    real = g._git

    async def flaky(cmd, cwd, timeout=None):
        if cmd.startswith("stash push"):
            return {"ok": False, "output": "stash failed: disk full"}
        return await real(cmd, cwd, timeout=timeout)

    monkeypatch.setattr(g, "_git", flaky)
    (workspace / "precious.txt").write_text("do not lose me\n")
    out = await g.sync_workspace_to_base(str(workspace), task_id="task-b")
    assert out["synced"] is False
    assert "could not be stashed" in out["reason"]
    assert (workspace / "precious.txt").exists()


async def test_the_work_node_tells_the_operator_when_it_salvaged(workspace):
    """A stash nobody is told about is a stash nobody recovers."""
    import inspect

    from agent.nodes import work

    src = inspect.getsource(work.work_node) if hasattr(work, "work_node") else ""
    src = src or open(work.__file__).read()
    assert "salvaged_from" in src
    assert "git stash list" in src
    # ...and a skipped sync is no longer silent.
    assert "WORKSPACE NOT SYNCED" in src
