"""Each task gets its own workspace (agent/workspaces.py).

Until 2026-09-23 a project had one worktree and its tasks took turns in it.
These pin what replaces that: a worktree per task, on the task's branch, filled
from the project's workspace -- dependency trees hardlinked, build output
copied -- and never able to reach production through a link or a mount.
"""
import os
import subprocess

import pytest

from agent import workspaces as ws
from agent.config import PROJECTS

pytestmark = [pytest.mark.real_workspaces]

TASK_A = "11111111-2222-3333-4444-555555555555"
TASK_B = "66666666-7777-8888-9999-000000000000"


def _run(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null",
                 "GIT_CONFIG_SYSTEM": "/dev/null"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def project(tmp_path):
    """live on main; the project's workspace a worktree of it, with the kind
    of ignored content a real one has."""
    live = tmp_path / "live"
    live.mkdir()
    _run(["init", "-q", "-b", "main"], live)
    _run(["config", "user.email", "t@x"], live)
    _run(["config", "user.name", "t"], live)
    (live / ".gitignore").write_text("node_modules/\ndist/\n.uploads/\n")
    (live / "app.js").write_text("v1\n")
    _run(["add", "-A"], live)
    _run(["commit", "-q", "-m", "base"], live)
    template = tmp_path / "workspaces" / "demo"
    template.parent.mkdir()
    _run(["worktree", "add", "-q", "--detach", str(template), "main"], live)
    nm = template / "node_modules"
    (nm / "lib").mkdir(parents=True)
    (nm / "lib" / "index.js").write_text("module.exports = 1\n")
    (nm / ".package-lock.json").write_text("{}\n")
    (nm / ".cache" / "babel").mkdir(parents=True)
    (nm / ".cache" / "babel" / "x").write_text("cached\n")
    (template / "dist").mkdir()
    (template / "dist" / "bundle.js").write_text("built\n")
    (template / ".uploads" / "b1").mkdir(parents=True)
    (template / ".uploads" / "b1" / "spec.pdf").write_text("pdf\n")
    PROJECTS["demo"] = {"live": str(live), "sandbox": str(template)}
    yield live, template
    PROJECTS.pop("demo", None)


def _inode(p):
    return os.stat(p).st_ino


async def test_a_task_gets_its_own_worktree_on_its_own_branch(project):
    live, template = project
    out = await ws.ensure("demo", TASK_A)
    path = out["path"]
    assert out["created"] and path == ws.task_workspace_path("demo", TASK_A)
    assert path.startswith(str(template.parent / ".tasks" / "demo"))
    assert _run(["rev-parse", "--abbrev-ref", "HEAD"], path).stdout.strip() == f"agent/{TASK_A}"
    assert open(os.path.join(path, "app.js")).read() == "v1\n"


async def test_dependencies_are_linked_and_build_output_is_copied(project):
    _, template = project
    path = (await ws.ensure("demo", TASK_A))["path"]
    # Hardlinked: same inode, no extra disk.
    assert _inode(os.path.join(path, "node_modules/lib/index.js")) == _inode(template / "node_modules/lib/index.js")
    # Rewritten in place by npm, so a real copy -- and caches left out.
    assert _inode(os.path.join(path, "node_modules/.package-lock.json")) != \
        _inode(template / "node_modules/.package-lock.json")
    assert not os.path.exists(os.path.join(path, "node_modules/.cache"))
    # Build output and uploads: real copies, so one task's build cannot
    # rewrite another's.
    assert _inode(os.path.join(path, "dist/bundle.js")) != _inode(template / "dist/bundle.js")
    assert open(os.path.join(path, ".uploads/b1/spec.pdf")).read() == "pdf\n"
    # Nothing carried over shows up as a change.
    assert _run(["status", "--porcelain"], path).stdout.strip() == ""


async def test_two_tasks_never_see_each_others_edits(project):
    a = (await ws.ensure("demo", TASK_A))["path"]
    b = (await ws.ensure("demo", TASK_B))["path"]
    assert a != b
    open(os.path.join(a, "app.js"), "w").write("task A\n")
    open(os.path.join(a, "dist/bundle.js"), "w").write("A's build\n")
    assert open(os.path.join(b, "app.js")).read() == "v1\n"
    assert open(os.path.join(b, "dist/bundle.js")).read() == "built\n"


async def test_a_second_ensure_hands_back_the_same_tree_with_its_work(project):
    a = (await ws.ensure("demo", TASK_A))["path"]
    open(os.path.join(a, "app.js"), "w").write("work in progress\n")
    again = await ws.ensure("demo", TASK_A)
    assert again["path"] == a and again["created"] is False
    assert open(os.path.join(a, "app.js")).read() == "work in progress\n"


async def test_attachments_added_later_reach_an_existing_workspace(project):
    """Uploaded with a resume: stored in the project's workspace, needed in
    the task's."""
    _, template = project
    a = (await ws.ensure("demo", TASK_A))["path"]
    (template / ".uploads" / "b2").mkdir()
    (template / ".uploads" / "b2" / "later.txt").write_text("new attachment\n")
    again = await ws.ensure("demo", TASK_A)
    assert again["uploads"] == ["b2"]
    assert open(os.path.join(a, ".uploads/b2/later.txt")).read() == "new attachment\n"


async def test_removal_keeps_the_branch_and_its_commits(project):
    live, _ = project
    a = (await ws.ensure("demo", TASK_A))["path"]
    open(os.path.join(a, "app.js"), "w").write("committed\n")
    _run(["commit", "-qam", "work"], a)
    assert (await ws.remove("demo", TASK_A))["ok"]
    assert not os.path.exists(a)
    assert _run(["show", f"agent/{TASK_A}:app.js"], live).stdout == "committed\n"
    # ...and a resume gets it back, on its branch.
    b = (await ws.ensure("demo", TASK_A))["path"]
    assert open(os.path.join(b, "app.js")).read() == "committed\n"


async def test_a_task_from_the_shared_workspace_era_is_moved_out_with_its_edits(project):
    """Its branch is checked out in the project's workspace, with uncommitted
    edits. git will not check a branch out twice, so the project workspace
    lets go of it -- the edits stashed under the task's name, where the work
    node's reclaim puts them back."""
    from agent.tools.git import reclaim_own_stash

    live, template = project
    _run(["checkout", "-q", "-b", f"agent/{TASK_A}"], template)
    (template / "app.js").write_text("uncommitted edit\n")
    out = await ws.ensure("demo", TASK_A)
    assert out["moved_from"] == str(template) and out["moved_uncommitted_work"]
    assert _run(["rev-parse", "--abbrev-ref", "HEAD"], template).stdout.strip() == "HEAD"
    reclaimed = await reclaim_own_stash(out["path"], TASK_A)
    assert reclaimed["reclaimed"], reclaimed
    assert open(os.path.join(out["path"], "app.js")).read() == "uncommitted edit\n"


def test_only_the_projects_own_directories_map_to_it(project):
    _, template = project
    task_dir = os.path.join(ws.tasks_root(str(template)), TASK_A)
    os.makedirs(task_dir)
    assert ws.project_for_path(str(template))[0] == "demo"
    assert ws.project_for_path(task_dir)[0] == "demo"
    # Not a directory inside a task workspace, not a neighbour.
    assert ws.project_for_path(os.path.join(task_dir, "sub")) is None
    assert ws.project_for_path(str(template.parent)) is None


async def test_a_mount_is_never_copied_and_never_deleted_through(project, monkeypatch):
    """A read-only bind mount of live data inside the project workspace is
    mounted again, never copied -- a hardlink would reach production's files --
    and a workspace is not deleted while anything is still mounted in it."""
    _, template = project
    (template / "dist" / "data").mkdir()
    (template / "dist" / "data" / "big.json").write_text("live data\n")
    real_template = os.path.realpath(template)
    monkeypatch.setattr(ws, "mounts_under", lambda p: [os.path.join(real_template, "dist", "data")]
                        if os.path.realpath(p) == real_template else [])
    mounted = []
    monkeypatch.setattr(ws, "_run", lambda cmd, timeout=60: (mounted.append(cmd) or (True, "")))
    path = (await ws.ensure("demo", TASK_A))["path"]
    assert os.path.isdir(os.path.join(path, "dist", "data"))
    assert not os.path.exists(os.path.join(path, "dist", "data", "big.json"))
    assert ["mount", "--bind", os.path.join(real_template, "dist", "data"),
            os.path.join(path, "dist", "data")] in mounted

    # Removal: the mount will not go away, so the workspace stays.
    monkeypatch.setattr(ws, "mounts_under", lambda p: [os.path.join(path, "dist", "data")])
    monkeypatch.setattr(ws, "_run", lambda cmd, timeout=60: (False, "busy"))
    out = await ws.remove("demo", TASK_A)
    assert out["ok"] is False and "still mounted" in out["reason"]
    assert os.path.exists(path)


async def test_generated_code_is_regenerated_once_per_schema(project, monkeypatch):
    """A client generated from an older schema is regenerated, in the sandbox,
    and stamped with the schema's hash -- so the next look is free, and a
    workspace filled from this one starts current."""
    _, template = project
    (template / "prisma").mkdir()
    (template / "prisma" / "schema.prisma").write_text("enum A { X }\n")
    calls = []

    async def fake_sandboxed(cmd, cwd, timeout=None, network=None):
        calls.append((cmd, cwd, network))
        return {"ok": True, "output": ""}

    monkeypatch.setattr("agent.tools.sandbox.run_shell_sandboxed", fake_sandboxed)
    rules = [{"dir": "dist", "schemaFile": "prisma/schema.prisma",
              "regenerate": {"dir": ".", "cmd": "npx", "args": ["prisma", "generate"]}}]
    assert await ws.refresh_generated(str(template), rules) == [{"dir": "dist", "ok": True}]
    assert calls == [("cd . && npx prisma generate", str(template), "none")]
    assert await ws.refresh_generated(str(template), rules) == []          # stamped: nothing to do
    (template / "prisma" / "schema.prisma").write_text("enum A { X Y }\n")
    assert await ws.refresh_generated(str(template), rules) == [{"dir": "dist", "ok": True}]
    # A rule pointing outside the workspace is ignored, not run.
    bad = [{**rules[0], "dir": "../../etc"}]
    assert await ws.refresh_generated(str(template), bad) == []


# --- the sandbox and git checks recognise a task's workspace ---------------

async def test_the_sandbox_allows_the_projects_paths_for_a_task_workspace(project):
    from agent.tools.sandbox import _mount_allow_roots

    live, template = project
    path = (await ws.ensure("demo", TASK_A))["path"]
    roots = _mount_allow_roots(path)
    assert os.path.realpath(live) in roots and os.path.realpath(template) in roots


async def test_git_still_refuses_a_rewritten_pointer_in_a_task_workspace(project, tmp_path):
    from agent.tools.git import _trusted_git_dir_error

    path = (await ws.ensure("demo", TASK_A))["path"]
    assert _trusted_git_dir_error(path) is None
    open(os.path.join(path, ".git"), "w").write(f"gitdir: {tmp_path}/elsewhere\n")
    assert "Refusing" in (_trusted_git_dir_error(path) or "")


def test_a_project_can_name_its_own_sandbox_environment(project):
    from agent.tools.sandbox import SANDBOX_IMAGE, sandbox_environment_for

    _, template = project
    assert sandbox_environment_for(str(template)) == (SANDBOX_IMAGE, {})
    PROJECTS["demo"]["stack"] = "go"
    image, env = sandbox_environment_for(str(template))
    assert image.startswith("golang") and env.get("GOCACHE")
    PROJECTS["demo"]["sandbox_image"] = "swebench/instance:1"
    assert sandbox_environment_for(str(template))[0] == "swebench/instance:1"


def test_a_command_starts_in_sh_and_uses_bash_where_the_image_has_it():
    from agent.tools.sandbox import _shell

    argv = _shell("echo hi")
    assert argv[:2] == ["sh", "-c"] and argv[-1] == "echo hi"
    assert "exec bash" in argv[2] and "exec sh" in argv[2]
