"""Changing a project's path after onboarding (2026-09-28: the desktop app's
projects folder moved and the only way to follow was remove and re-add).
The new path is held to onboarding's rules, must be a checkout of the same
repository, and nothing on disk moves."""
import json
import subprocess

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
