"""Changing a project's path after onboarding (2026-09-28: the desktop app's
projects folder moved and the only way to follow was remove and re-add).
The new path is held to onboarding's rules, must be a checkout of the same
repository, and nothing on disk moves."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent.config as agent_config
import agent.server as srv
from agent import audit, provisioning
from agent.auth import User

ADMIN = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)


def _repo(path, origin=None):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", path], check=True)
    if origin:
        subprocess.run(["git", "-C", path, "remote", "add", "origin", origin], check=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    roots = tmp_path / "code"
    old, new = roots / "shop", roots / "elsewhere" / "shop"
    _repo(old, "https://github.com/acme/shop.git")
    _repo(new, "git@github.com:Acme/shop.git")
    cfg = tmp_path / "projects.json"
    cfg.write_text(json.dumps({"projects": {"shop": {"live": str(old), "sandbox": str(old), "ship": "pr"}}}))
    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", cfg)
    monkeypatch.setitem(agent_config.PROJECTS, "shop", {"live": str(old), "sandbox": str(old), "ship": "pr"})
    monkeypatch.setattr(provisioning, "allowed_roots", lambda: [str(roots)])
    monkeypatch.setattr(provisioning, "_agent_own_roots", lambda: [])
    recorded = []

    async def record(store, **kw):
        recorded.append(kw)

    monkeypatch.setattr(audit, "record", record)
    monkeypatch.setattr(agent_config, "reload_projects",
                        lambda: agent_config.PROJECTS.__setitem__("shop", json.loads(cfg.read_text())["projects"]["shop"]))
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: ADMIN)

    async def idle(app):
        return set()

    from agent.routers import projects as pr
    monkeypatch.setattr(pr, "_running_repos", idle)
    return {"cfg": cfg, "old": old, "new": new, "roots": roots, "audit": recorded}


def test_the_path_moves_the_sandbox_follows_and_the_change_is_audited(world):
    c = TestClient(srv.app)
    r = c.post("/api/projects/shop/move", json={"live": str(world["new"])})
    assert r.status_code == 200, r.text
    assert r.json()["live"] == str(world["new"].resolve()) and r.json()["sandbox"] == str(world["new"].resolve())
    saved = json.loads(world["cfg"].read_text())["projects"]["shop"]
    assert saved["live"] == str(world["new"].resolve()) and saved["ship"] == "pr", "other settings are kept"
    assert world["old"].exists() and world["new"].exists(), "nothing on disk moved"
    assert world["audit"][-1]["action"] == "project.move" and world["audit"][-1]["target"] == "shop"
    r = c.post("/api/projects/shop/move", json={"live": str(world["new"])})
    assert r.status_code == 200 and r.json()["unchanged"] is True


def test_a_checkout_of_another_repository_is_refused(world):
    other = world["roots"] / "other"
    _repo(other, "https://github.com/acme/other.git")
    r = TestClient(srv.app).post("/api/projects/shop/move", json={"live": str(other)})
    assert r.status_code == 409 and "acme/other" in r.json()["detail"]
    assert json.loads(world["cfg"].read_text())["projects"]["shop"]["live"] == str(world["old"])


def test_onboardings_rules_still_apply(world, tmp_path):
    c = TestClient(srv.app)
    outside = tmp_path / "outside" / "shop"
    _repo(outside)
    assert c.post("/api/projects/shop/move", json={"live": str(outside)}).status_code == 400
    plain = world["roots"] / "plain"
    plain.mkdir()
    r = c.post("/api/projects/shop/move", json={"live": str(plain)})
    assert r.status_code == 400 and "not a git checkout" in r.json()["detail"]
    assert c.post("/api/projects/nope/move", json={"live": str(world["new"])}).status_code == 404


def test_a_path_another_project_uses_is_refused(world):
    other_live = world["roots"] / "other"
    _repo(other_live, "https://github.com/acme/shop.git")
    world["cfg"].write_text(json.dumps({"projects": {
        "shop": {"live": str(world["old"]), "sandbox": str(world["old"])},
        "twin": {"live": str(other_live), "sandbox": str(other_live)}}}))
    agent_config.PROJECTS["twin"] = {"live": str(other_live), "sandbox": str(other_live)}
    try:
        r = TestClient(srv.app).post("/api/projects/shop/move", json={"live": str(other_live)})
    finally:
        agent_config.PROJECTS.pop("twin", None)
    assert r.status_code == 409 and "twin" in r.json()["detail"]


# --- a project whose workspace is a worktree of the live checkout ---------
#
# That is the shape onboarding produces on a host install and in the bundle
# (provisioning.create_worktree under sandbox_root). A worktree belongs to
# one repository, so after a move it still pointed at the old checkout and
# tools/git.py refused every command against it (2026-09-29).

class _Store:
    def __init__(self, tasks=None):
        self.tasks = tasks or {}

    async def asearch(self, ns, limit=100, offset=0):
        items = list(self.tasks.items())[offset:offset + limit] if tuple(ns) == ("tasks", "shop") else []
        return [type("Item", (), {"key": k, "value": v})() for k, v in items]


@pytest.fixture
def worktree_world(world, monkeypatch):
    ws = world["roots"] / ".workspaces" / "shop"
    # A fresh CI runner has no git identity; the commit must not depend on one.
    ident = ["-c", "user.name=tektonix-tests", "-c", "user.email=tests@tektonix.invalid"]
    subprocess.run(["git", *ident, "-C", world["old"], "commit", "-q", "--allow-empty", "-m", "start"], check=True)
    subprocess.run(["git", "-C", world["old"], "worktree", "add", "-q", str(ws), "-b", "agent-base"], check=True)
    entry = {"live": str(world["old"]), "sandbox": str(ws), "ship": "pr"}
    world["cfg"].write_text(json.dumps({"projects": {"shop": entry}}))
    agent_config.PROJECTS["shop"] = dict(entry)
    subprocess.run(["git", *ident, "-C", world["new"], "commit", "-q", "--allow-empty", "-m", "start"], check=True)
    store = _Store()
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setattr(provisioning, "sandbox_root", lambda: str(world["roots"] / ".workspaces"))
    world.update(ws=ws, store=store)
    return world


def _worktrees_of(live):
    out = subprocess.run(["git", "-C", live, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


def test_a_separate_workspace_is_rebuilt_against_the_new_checkout(worktree_world):
    from agent.tools.git import _trusted_git_dir_error
    w = worktree_world
    r = TestClient(srv.app).post("/api/projects/shop/move", json={"live": str(w["new"])})
    assert r.status_code == 200, r.text
    assert r.json()["workspace_rebuilt"] is True
    assert r.json()["sandbox"] == str(w["ws"]), "the workspace path itself does not change"
    assert w["ws"].is_dir() and (w["ws"] / ".git").is_file()
    pointer = (w["ws"] / ".git").read_text().strip().removeprefix("gitdir:").strip()
    assert str(w["new"].resolve() / ".git") in str(os.path.realpath(pointer))
    assert str(w["ws"].resolve()) not in [str(Path(p).resolve()) for p in _worktrees_of(w["old"])], \
        "the old checkout still registers the workspace"
    assert agent_config.PROJECTS["shop"]["live"] == str(w["new"].resolve())
    assert _trusted_git_dir_error(str(w["ws"])) is None, "git would still refuse to run in the workspace"


def test_a_task_workspace_under_the_old_checkout_blocks_the_move(worktree_world):
    w = worktree_world
    task_dir = w["roots"] / ".workspaces" / ".tasks" / "shop" / "abc123"
    task_dir.mkdir(parents=True)
    r = TestClient(srv.app).post("/api/projects/shop/move", json={"live": str(w["new"])})
    assert r.status_code == 409 and "task workspace" in r.json()["detail"]
    assert json.loads(w["cfg"].read_text())["projects"]["shop"]["live"] == str(w["old"])
    assert str(w["ws"].resolve()) in [str(Path(p).resolve()) for p in _worktrees_of(w["old"])], \
        "the workspace was detached despite the refusal"


@pytest.mark.parametrize("status", ["awaiting_approval", "awaiting_merge", "escalated"])
def test_a_parked_task_blocks_the_move(worktree_world, status):
    w = worktree_world
    w["store"].tasks["t1"] = {"task_id": "t1", "status": status}
    w["store"].tasks["t2"] = {"task_id": "t2", "status": "done"}
    r = TestClient(srv.app).post("/api/projects/shop/move", json={"live": str(w["new"])})
    assert r.status_code == 409 and "1 task(s) waiting" in r.json()["detail"]
    assert json.loads(w["cfg"].read_text())["projects"]["shop"]["live"] == str(w["old"])


def test_finished_tasks_do_not_block_the_move(worktree_world):
    w = worktree_world
    w["store"].tasks["t2"] = {"task_id": "t2", "status": "done"}
    w["store"].tasks["t3"] = {"task_id": "t3", "status": "stopped"}
    assert TestClient(srv.app).post("/api/projects/shop/move", json={"live": str(w["new"])}).status_code == 200


def test_the_deploy_key_and_the_slug_cache_follow_the_move(world, monkeypatch, tmp_path):
    from agent import deploy_keys
    from agent.tools import github_tools
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setattr(deploy_keys, "KEYS_DIR", keys)
    kf = keys / "shop.key"
    kf.write_text("not really a key\n")
    deploy_keys.configure_repo(str(world["old"]), kf)
    github_tools._slug_cache["shop"] = "acme/shop"
    r = TestClient(srv.app).post("/api/projects/shop/move", json={"live": str(world["new"])})
    assert r.status_code == 200 and r.json()["deploy_key_followed"] is True
    out = subprocess.run(["git", "-C", world["new"], "config", "--get", "core.sshCommand"],
                         capture_output=True, text=True).stdout
    assert str(kf) in out, "the new checkout pushes without the project's key"
    assert "shop" not in github_tools._slug_cache, "the slug from the old checkout is still cached"
