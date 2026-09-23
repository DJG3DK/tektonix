"""A parked task gets its own stashed work back when it resumes.

The live shape, 2026-09-23: task A paused mid-turn for an approval with its
edits uncommitted; task B took the workspace and its sync stashed A's edits as
"left dirty by A"; A resumed into B's tree without them, redid the work,
paused again, and lost the redo the same way. These run that sequence
against a real repository.
"""
import asyncio
import subprocess

import pytest

from agent.tools import git as g

A = "aaaaaaaa-0000-0000-0000-000000000001"
B = "bbbbbbbb-0000-0000-0000-000000000002"


def _run(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


def _commit(repo, files, msg):
    for name, body in files.items():
        (repo / name).write_text(body)
    _run(["add", "-A"], repo)
    _run(["commit", "-q", "-m", msg], repo)


@pytest.fixture
def ws(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    _run(["init", "-q", "-b", "main"], live)
    _run(["config", "user.email", "t@x"], live)
    _run(["config", "user.name", "t"], live)
    _commit(live, {"app.py": "v1\n", "other.py": "o1\n"}, "base")
    ws = tmp_path / "ws"
    _run(["worktree", "add", "-q", "--detach", str(ws), "main"], live)
    return live, ws


def _go(coro):
    return asyncio.run(coro)


def _head(ws):
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], ws).strip()


def test_a_paused_tasks_edits_come_back_after_another_task_ran(ws):
    live, w = ws
    _go(g.sync_workspace_to_base(str(w), task_id=A))
    (w / "app.py").write_text("A's half-finished edit\n")            # A pauses here, uncommitted
    _go(g.sync_workspace_to_base(str(w), task_id=B))                  # B takes the workspace
    assert (w / "app.py").read_text() == "v1\n"                       # A's edit is stashed
    (w / "other.py").write_text("B's edit\n")                         # B leaves its own mess

    out = _go(g.reclaim_own_stash(str(w), A))
    assert out["reclaimed"] is True
    assert (w / "app.py").read_text() == "A's half-finished edit\n"   # A's work is back
    assert (w / "other.py").read_text() == "o1\n"                     # B's is set aside...
    assert f"left dirty by {B}" in _run(["stash", "list"], w)         # ...recoverably
    assert _go(g._workspace_owner(str(w))) == A
    assert f"left dirty by {A}" not in _run(["stash", "list"], w)     # popped, not copied


def test_work_on_the_tasks_branch_comes_back_on_that_branch(ws):
    live, w = ws
    branch = g.task_branch_name(A)
    _run(["checkout", "-q", "-b", branch], w)
    _commit(w, {"app.py": "A committed\n"}, "A's first pass")
    _go(g._claim_workspace(str(w), A))
    (w / "app.py").write_text("A's second pass, uncommitted\n")
    _go(g.sync_workspace_to_base(str(w), task_id=B))                  # stashed, detached to main

    out = _go(g.restore_task_workspace(str(w), A))                    # A's later pass starts
    assert out["reclaimed"]["reclaimed"] is True
    assert _head(w) == branch
    assert (w / "app.py").read_text() == "A's second pass, uncommitted\n"


def test_nothing_to_reclaim_changes_nothing(ws):
    live, w = ws
    (w / "other.py").write_text("someone's edit\n")
    out = _go(g.reclaim_own_stash(str(w), A))
    assert out == {"ok": True, "reclaimed": False}
    assert (w / "other.py").read_text() == "someone's edit\n"


def test_a_stash_that_will_not_apply_is_kept(ws, monkeypatch):
    """Reclaim checks out the stash's own base first, so a pop there applies
    in any ordinary case. When it does not -- a disk error, a hook, something
    unforeseen -- the stash stays and the result says so: nothing is dropped
    that was not applied."""
    live, w = ws
    _go(g.sync_workspace_to_base(str(w), task_id=A))
    (w / "app.py").write_text("A's edit\n")
    _go(g.sync_workspace_to_base(str(w), task_id=B))

    real = g._git

    async def failing_pop(cmd, root, timeout=30):
        if cmd.startswith("stash pop"):
            return {"ok": False, "output": "error: simulated failure"}
        return await real(cmd, root, timeout=timeout)

    monkeypatch.setattr(g, "_git", failing_pop)
    out = _go(g.reclaim_own_stash(str(w), A))
    monkeypatch.setattr(g, "_git", real)
    assert out["ok"] is False and out["reclaimed"] is False
    assert "kept" in out["reason"]
    assert f"left dirty by {A}" in _run(["stash", "list"], w)


def test_resuming_after_an_approval_reclaims_first():
    """The path the live case took: an approval resumes the paused turn,
    which skips the ordinary restore, so the reclaim has to be on it."""
    import inspect

    from agent.nodes import work
    src = inspect.getsource(work.work_node)
    start = src.index('if state.get("approval_decision"):')
    branch = src[start:src.index("elif not (", start)]
    assert "reclaim_own_stash" in branch
