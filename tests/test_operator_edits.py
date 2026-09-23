"""The final-look panel's hand edit: read from the branch, applied under the
task's own run, and sent through the same gate as the agent's work."""
import asyncio
import os
import subprocess

import pytest

import agent.nodes.verify_and_ship as vas
import agent.server as server
import agent.task_diff as td
from agent.tools import git as g

TASK = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
BRANCH = f"agent/{TASK}"


def _run(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _commit(repo, files, msg):
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    _run(["add", "-A"], repo)
    _run(["commit", "-q", "-m", msg], repo)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """live main, a workspace worktree, and the task's committed branch -- with
    the workspace since moved on to another task's checkout."""
    live = tmp_path / "live"
    live.mkdir()
    _run(["init", "-q", "-b", "main"], live)
    _run(["config", "user.email", "t@x"], live)
    _run(["config", "user.name", "t"], live)
    _commit(live, {"src/app.js": "const a = 1;\n", "README.md": "hi\n"}, "base")
    ws = tmp_path / "ws"
    _run(["worktree", "add", "-q", str(ws), "-b", BRANCH], live)
    _commit(ws, {"src/app.js": "const a = 2;\n"}, "task")
    _run(["checkout", "-q", "--detach", "main"], ws)   # another task took the workspace
    monkeypatch.setitem(td.PROJECTS, "proj", {"live": str(live), "sandbox": str(ws)})
    monkeypatch.setitem(vas.PROJECTS, "proj", {"live": str(live), "sandbox": str(ws)})
    tip = _run(["rev-parse", BRANCH], live).stdout.strip()
    return live, ws, tip


# --- reading ------------------------------------------------------------------

def test_the_file_is_read_from_the_branch_not_the_workspace(project):
    live, ws, tip = project
    out = asyncio.run(td.read_task_file("proj", BRANCH, "src/app.js"))
    assert out["modified"] == "const a = 2;\n"        # the task's version
    assert out["original"] == "const a = 1;\n"        # before the task
    assert out["sha"] == tip
    assert (ws / "src/app.js").read_text() == "const a = 1;\n"   # workspace is elsewhere


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", ".git/config", "src/../../x"])
def test_paths_outside_the_repo_are_refused(project, bad):
    with pytest.raises(ValueError):
        asyncio.run(td.read_task_file("proj", BRANCH, bad))


def test_the_diff_panel_shows_the_task_even_when_the_workspace_moved_on(project):
    out = asyncio.run(td.collect_task_diff("proj", task_branch=BRANCH))
    assert [f["path"] for f in out["files"]] == ["src/app.js"]
    assert out["branch"] == BRANCH


# --- applying -----------------------------------------------------------------

def _edits(tip, files=None):
    return {"base_sha": tip, "files": files or [{"path": "src/app.js", "content": "const a = 3;\n"}],
            "note": "typo", "by": "op@example.com"}


def test_an_edit_lands_on_the_task_branch_checkout(project):
    live, ws, tip = project
    (ws / "stray.txt").write_text("another task's debris\n")
    out = asyncio.run(vas._apply_operator_edits({"task_id": TASK}, str(ws), _edits(tip)))
    assert out == {"ok": True}
    assert _run(["rev-parse", "--abbrev-ref", "HEAD"], ws).stdout.strip() == BRANCH
    assert (ws / "src/app.js").read_text() == "const a = 3;\n"
    assert not (ws / "stray.txt").exists()                        # stashed, not inherited
    assert "debris" in _run(["stash", "show", "-p", "--include-untracked"], ws).stdout
    assert asyncio.run(g._workspace_owner(str(ws))) == TASK      # a loop-back keeps the edit


def test_an_edit_against_a_moved_branch_is_refused(project):
    live, ws, tip = project
    out = asyncio.run(vas._apply_operator_edits({"task_id": TASK}, str(ws), _edits("0" * 40)))
    assert out["ok"] is False and "moved" in out["reason"]


def test_an_edit_through_a_symlink_is_refused(project, tmp_path):
    live, ws, tip = project
    outside = tmp_path / "outside"
    outside.mkdir()
    _run(["checkout", "-q", BRANCH], ws)
    os.symlink(outside, ws / "linked")
    _run(["add", "-A"], ws)
    _run(["commit", "-q", "-m", "link"], ws)
    tip = _run(["rev-parse", BRANCH], live).stdout.strip()
    out = asyncio.run(vas._apply_operator_edits(
        {"task_id": TASK}, str(ws), _edits(tip, [{"path": "linked/evil.js", "content": "x"}])))
    assert out["ok"] is False
    assert not (outside / "evil.js").exists()


def test_the_node_applies_then_runs_the_normal_gate(project, monkeypatch):
    live, ws, tip = project
    seen = {}

    async def inner(state, repo, repo_root, store):
        seen["edit"] = state.get("_operator_edit")
        seen["tree"] = (ws / "src/app.js").read_text()
        return {"pending_merge_approval": {"sha": "new"}}

    monkeypatch.setattr(vas, "_verify_and_ship_inner", inner)
    state = {"task_id": TASK, "repo": "proj", "iteration_count": 0, "max_iterations": 40,
             "operator_edits": _edits(tip)}
    out = asyncio.run(vas.verify_and_ship_node(state, None, None))
    assert seen["tree"] == "const a = 3;\n"
    assert seen["edit"]["by"] == "op@example.com"
    assert out["operator_edits"] is None
    assert out["pending_merge_approval"] == {"sha": "new"}


# --- the endpoint -----------------------------------------------------------------

class _Ck:
    def __init__(self, values):
        self.values = values


class _Graph:
    def __init__(self, values):
        self.values, self.updates = values, []

    async def aget_state(self, cfg):
        return _Ck(self.values)

    async def aupdate_state(self, cfg, patch, **kw):
        self.updates.append((dict(patch), kw.get("as_node")))


@pytest.fixture
def wired(monkeypatch):
    class _User:
        email = "op@example.com"

    async def _record(*a, **k):
        return None

    async def _stream(*a, **k):
        return None

    def _wire(values):
        gr = _Graph(values)
        monkeypatch.setattr(server.app.state, "graph", gr, raising=False)
        monkeypatch.setattr(server.app.state, "store", object(), raising=False)
        monkeypatch.setattr(server, "check_repo_access", lambda *a, **k: None)
        monkeypatch.setattr(server.audit, "record", _record)
        monkeypatch.setattr(server, "_stream_graph", _stream)
        return gr, _User()
    return _wire


def _req(sha="abc", path="src/app.js"):
    return server.OperatorEditRequest(base_sha=sha, files=[{"path": path, "content": "x"}], note="n")


async def _submit(req, user):
    try:
        return await server.submit_operator_edits("t-9", req, user=user)
    finally:
        await asyncio.sleep(0)
        server._running_tasks.pop("t-9", None)


def test_a_saved_edit_goes_to_the_gate_not_to_the_agent(wired):
    gr, user = wired({"repo": "proj", "goal": "g", "pending_merge_approval": {"sha": "abc"},
                      "merge_approved_sha": "abc"})
    asyncio.run(_submit(_req(), user))
    patch, as_node = gr.updates[-1]
    assert as_node == "work"                      # next node: verify_and_ship
    assert patch["operator_edits"]["files"] == [{"path": "src/app.js", "content": "x"}]
    assert patch["pending_merge_approval"] is None
    assert patch["merge_approved_sha"] is None    # the old approval never ships the edit


def test_no_edit_unless_the_task_is_waiting_for_the_final_look(wired):
    gr, user = wired({"repo": "proj", "goal": "g", "pending_merge_approval": None})
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(_submit(_req(), user))
    assert e.value.status_code == 409


def test_no_edit_against_a_commit_other_than_the_one_shown(wired):
    gr, user = wired({"repo": "proj", "goal": "g", "pending_merge_approval": {"sha": "newer"}})
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(_submit(_req(sha="abc"), user))
    assert e.value.status_code == 409


def test_a_bad_path_is_refused_at_the_door(wired):
    gr, user = wired({"repo": "proj", "goal": "g", "pending_merge_approval": {"sha": "abc"}})
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(_submit(_req(path="../../etc/passwd"), user))
    assert e.value.status_code == 400
    assert gr.updates == []
