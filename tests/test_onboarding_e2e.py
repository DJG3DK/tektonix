"""End-to-end onboarding: a real directory becomes a project the whole system
can see, driven through the real HTTP endpoints.

This test exists because the failure mode of onboarding is partial success --
a project written to projects.json that the running process cannot see, or
that the reviewer and deploy services ignore. Each of those looks like it
worked. So the assertions here follow one project all the way through:

    detect -> operator choices -> provision -> live in-process
           -> visible to the reviewer (JS) -> visible to deploy (JS)

Nothing is mocked except authentication and the cartographer's model call.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import config as agent_config
from agent import provisioning as prov
from agent.auth import User

REPO_ROOT = Path(__file__).resolve().parent.parent

_ADMIN = User(
    id=1, email="admin@example.com", role="admin", allowed_repos=None,
    totp_enabled=True, must_change_password=False, auto_approve_commands=False,
    require_merge_review=True,
)
_NON_ADMIN = User(
    id=2, email="dev@example.com", role="user", allowed_repos=["something"],
    totp_enabled=True, must_change_password=False, auto_approve_commands=False,
    require_merge_review=True,
)


@pytest.fixture
def sample_repo(tmp_path):
    """A realistic project: npm scripts, a gitignored secret, and one test
    script that talks to a live service."""
    repo = tmp_path / "orders-api"
    (repo / "tests").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({
        "name": "orders-api",
        "scripts": {
            "lint": "eslint src",
            "typecheck": "tsc --noEmit",
            "test": "node tests/unit.js",
            "test:smoke": "node tests/smoke.js",
        },
    }))
    (repo / "package-lock.json").write_text("{}")
    (repo / ".gitignore").write_text(".env\nnode_modules\n")
    (repo / ".env").write_text("DATABASE_URL=postgres://localhost/orders\n")
    (repo / "tests" / "unit.js").write_text("if (1+1 !== 2) process.exit(1);\n")
    (repo / "tests" / "smoke.js").write_text(
        "await fetch('http://127.0.0.1:9000/admin/reset', {method:'POST'});\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point config + provisioning at throwaway paths so the real deployment's
    projects.json is never touched."""
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({"projects": {}}) + "\n")
    workspaces = tmp_path / "workspaces"

    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", projects_file)
    monkeypatch.setattr(agent_config, "PROJECTS", {}, raising=False)
    # server.py holds its own `from agent.config import PROJECTS` binding
    monkeypatch.setattr(srv, "PROJECTS", agent_config.PROJECTS, raising=False)
    # Both roots are server-owned config, not request input.
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(workspaces))
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    return {"projects_file": projects_file, "workspaces": workspaces}


def _stub_side_effects(monkeypatch):
    """Seeding and mapping talk to Postgres and an LLM; both are reported as
    steps and neither may fail the onboarding. Stub them so this test covers
    the wiring, not the network."""
    async def fake_seed_memory(repo, store, content):
        return None

    async def fake_cartographer(config, repo, store, force=False):
        return {"repo": repo, "mapped": True, "files": 4}

    import agent.deep_agent as da
    monkeypatch.setattr(da, "seed_memory", fake_seed_memory)
    monkeypatch.setattr(srv.cartographer, "run_cartographer", fake_cartographer)
    monkeypatch.setattr(srv.app.state, "store", object(), raising=False)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)


def test_full_onboarding_flow(sample_repo, wired, monkeypatch):
    _stub_side_effects(monkeypatch)
    client = TestClient(srv.app)

    # --- 1. detect -------------------------------------------------------
    res = client.post("/api/projects/detect", json={"path": str(sample_repo)})
    assert res.status_code == 200, res.text
    report = res.json()

    assert report["name"] == "orders-api"
    assert report["blockers"] == []
    assert report["package_manager"] == "npm"
    assert [c["name"] for c in report["checks"]] == ["typecheck", "lint"], \
        "the aggregate test script chains a live-service call, so it is not auto-enabled"
    risky = {c["value"]: c for c in report["risky_scripts"]}
    assert "test:smoke" in risky and risky["test:smoke"]["enabled"] is False
    assert report["db_env_file"] == ".env"

    # --- 2. operator confirms (rejecting the risky script) ---------------
    body = {
        "path": report["live"],
        "choices": {
            "secret_files": [c["value"] for c in report["secret_files"]],
            "read_only_mounts": [],
            "pm2_apps": [],
            "node_modules_dirs": report["node_modules_dirs"],
            "checks": report["checks"],          # risky script left out
            "build_steps": report["build_steps"],
            "db_env_file": report["db_env_file"],
        },
        "grant_access": False,
    }
    res = client.post("/api/projects/provision", json=body)
    assert res.status_code == 200, res.text
    result = res.json()
    assert result["ok"] is True
    by_step = {s["step"]: s for s in result["steps"]}
    assert by_step["worktree"]["ok"] and by_step["config"]["ok"]
    assert by_step["reload"]["ok"] and by_step["codebase-map"]["ok"]

    # --- 3. the worktree is real ----------------------------------------
    sandbox = Path(report["sandbox"])
    assert (sandbox / "package.json").is_file()
    assert (sandbox / ".git").is_file(), "a worktree's .git is a pointer file"

    # --- 4. live in THIS process, no restart ----------------------------
    assert "orders-api" in agent_config.PROJECTS
    assert agent_config.PROJECTS["orders-api"]["sandbox"] == str(sandbox)
    repos = client.get("/api/repos")
    assert repos.status_code == 200 and "orders-api" in repos.json()

    # --- 5. persisted config is what the operator confirmed -------------
    written = json.loads(wired["projects_file"].read_text())["projects"]["orders-api"]
    assert written["review"]["secretFiles"] == [".env"]
    assert [c["name"] for c in written["review"]["checks"]] == ["typecheck", "lint"]
    assert "test:smoke" not in json.dumps(written), \
        "a rejected script must not reach the config by any path"
    assert written["db_env_file"] == ".env"

    # --- 6. the JS services see it, with no edit to their own files -----
    node_probe = (
        "const {loadProjects} = require(%s);"
        "const r = loadProjects({}, {section:'review', file: %s});"
        "const d = loadProjects({}, {section:'deploy', file: %s});"
        "console.log(JSON.stringify({"
        "  reviewer: Object.keys(r),"
        "  checks: (r['orders-api'].checks||[]).map(c=>c.name),"
        "  secrets: r['orders-api'].secretFiles,"
        "  deployWorkspace: d['orders-api'].workspace }));"
    ) % (
        json.dumps(str(REPO_ROOT / "services" / "shared" / "projects-config.js")),
        json.dumps(str(wired["projects_file"])),
        json.dumps(str(wired["projects_file"])),
    )
    out = subprocess.run(["node", "-e", node_probe], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout)
    assert "orders-api" in seen["reviewer"]
    assert seen["checks"] == ["typecheck", "lint"]
    assert seen["secrets"] == [".env"]
    assert seen["deployWorkspace"] == str(sandbox)


def test_provision_refuses_a_duplicate_name(sample_repo, wired, monkeypatch):
    _stub_side_effects(monkeypatch)
    client = TestClient(srv.app)
    report = client.post("/api/projects/detect", json={"path": str(sample_repo)}).json()
    body = {"path": report["live"], "choices": {}, "grant_access": False}
    assert client.post("/api/projects/provision", json=body).json()["ok"] is True
    second = client.post("/api/projects/provision", json=body)
    assert second.status_code == 400
    assert "already configured" in second.json()["detail"]


def test_detect_rejects_a_non_git_directory(tmp_path, wired):
    plain = tmp_path / "notarepo"
    plain.mkdir()
    res = TestClient(srv.app).post("/api/projects/detect", json={"path": str(plain)})
    assert res.status_code == 200
    assert any("not a git repository" in b for b in res.json()["blockers"])


def test_detect_rejects_a_relative_path(wired):
    res = TestClient(srv.app).post("/api/projects/detect", json={"path": "relative/path"})
    assert res.status_code == 400
    assert "absolute" in res.json()["detail"]


def test_onboarding_is_admin_only(sample_repo, wired, monkeypatch):
    """These endpoints name host paths, copy credential files and create
    worktrees -- a non-admin must not reach any of them."""
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _NON_ADMIN)
    client = TestClient(srv.app)
    for method, path, payload in (
        ("get", "/api/projects", None),
        ("post", "/api/projects/detect", {"path": str(sample_repo)}),
        ("post", "/api/projects/provision", {"path": str(sample_repo), "choices": {}}),
    ):
        res = client.get(path) if method == "get" else client.post(path, json=payload)
        assert res.status_code in (401, 403), f"{path} allowed a non-admin: {res.status_code}"



def test_project_name_comes_from_the_directory_not_the_client(sample_repo, wired, monkeypatch):
    """`name` is not part of the request schema: it is the directory's own
    basename, so a caller cannot decide which key it lands under in
    projects.json (or which existing entry it would collide with)."""
    _stub_side_effects(monkeypatch)
    client = TestClient(srv.app)
    res = client.post("/api/projects/provision", json={
        "path": str(sample_repo),
        "name": "attacker-chosen",          # ignored: not in the schema
        "choices": {"checks": []},
        "grant_access": False,
    })
    assert res.status_code == 200, res.text
    written = json.loads(wired["projects_file"].read_text())["projects"]
    assert set(written) == {"orders-api"}


def test_a_dot_directory_is_rejected_as_a_project_name(tmp_path, wired, monkeypatch):
    """A hidden directory would produce a config key starting with '.'."""
    _stub_side_effects(monkeypatch)
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=hidden, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=hidden, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=hidden, check=True)
    (hidden / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=hidden, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=hidden, check=True)

    res = TestClient(srv.app).post("/api/projects/provision", json={
        "path": str(hidden), "choices": {}, "grant_access": False})
    assert res.status_code == 400
    assert "invalid project name" in res.json()["detail"]
    assert not (wired["workspaces"] / ".hidden").exists()


def test_reload_projects_mutates_the_shared_dict_in_place(tmp_path, monkeypatch):
    """Every module does `from agent.config import PROJECTS`, so reload must
    mutate that object -- rebinding would leave consumers on a stale copy and
    the new project invisible everywhere but config.py."""
    f = tmp_path / "projects.json"
    f.write_text(json.dumps({"projects": {"a": {"live": "/a", "sandbox": "/s/a"}}}))
    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", f)
    agent_config.PROJECTS.clear()
    agent_config.PROJECTS.update({"stale": {}})

    borrowed = agent_config.PROJECTS      # what a consumer module holds
    agent_config.reload_projects()

    assert borrowed is agent_config.PROJECTS, "reload rebound instead of mutating"
    assert set(borrowed) == {"a"}


@pytest.fixture
def go_repo(tmp_path):
    """A Go service with one honest suite and one that calls a live host --
    the same shape as the npm fixture above, in a stack that until now
    onboarded with no checks at all."""
    repo = tmp_path / "ledger-svc"
    (repo / "internal").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/ledger-svc\n\ngo 1.22\n")
    (repo / ".golangci.yml").write_text("linters:\n  enable: [govet]\n")
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("DATABASE_URL=postgres://localhost/ledger\n")
    (repo / "internal" / "sum_test.go").write_text(
        "package internal\n\nfunc TestSum(t *testing.T) { _ = 1 + 1 }\n")
    (repo / "settle_test.go").write_text(
        'package main\n\nimport "net/http"\n\n'
        'func TestSettle(t *testing.T) { http.Post("https://payments.example.com/settle", "", nil) }\n')
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_a_go_project_onboards_with_real_checks(go_repo, wired, monkeypatch):
    """The same journey as test_full_onboarding_flow, for a stack that used to
    arrive with an empty checks list -- which is how the review gate becomes a
    no-op: it passes every change because it runs nothing.
    """
    _stub_side_effects(monkeypatch)
    client = TestClient(srv.app)

    report = client.post("/api/projects/detect", json={"path": str(go_repo)}).json()
    assert report["blockers"] == []
    assert report["languages"] == ["go"]
    assert report["package_manager"] is None
    assert report["node_modules_dirs"] == [], "a Go repo has no node_modules to mount"
    assert [c["name"] for c in report["checks"]] == ["vet", "build", "lint"], \
        "the suite POSTs at a live host, so it is proposed flagged rather than as a check"
    assert [c["cmd"] for c in report["checks"]] == ["go", "go", "golangci-lint"]
    flagged = {c["value"]: c for c in report["risky_scripts"]}
    assert flagged["test"]["enabled"] is False
    assert "settle_test.go" in flagged["test"]["reason"]
    assert report["db_env_file"] == ".env"

    # The operator reads the flagged suite and accepts it: it is named, never
    # authored -- the client sends a name and the server substitutes its own
    # command (test_enabling_a_flagged_suite_runs_our_command_not_the_client_s
    # in test_provisioning.py holds that boundary directly).
    body = {
        "path": report["live"],
        "choices": {
            "secret_files": [c["value"] for c in report["secret_files"]],
            "read_only_mounts": [],
            "pm2_apps": [],
            "node_modules_dirs": [],
            "checks": [*report["checks"], {"name": "test"}],
            "build_steps": report["build_steps"],
            "db_env_file": report["db_env_file"],
        },
        "grant_access": False,
    }
    res = client.post("/api/projects/provision", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True

    written = json.loads(wired["projects_file"].read_text())["projects"]["ledger-svc"]
    checks = {c["name"]: c for c in written["review"]["checks"]}
    assert checks["test"]["cmd"] == "go" and checks["test"]["args"] == ["test", "./..."]
    assert checks["lint"]["cmd"] == "golangci-lint"
    assert written["review"]["secretFiles"] == [".env"]
    assert "nodeModulesDirs" not in written["review"]
    assert written["deploy"]["build"] == [{"dir": ".", "cmd": "go", "args": ["build", "./..."]}]

    # And the JS services read the same thing, with no edit to their own files.
    node_probe = (
        "const {loadProjects} = require(%s);"
        "const r = loadProjects({}, {section:'review', file: %s});"
        "const d = loadProjects({}, {section:'deploy', file: %s});"
        "console.log(JSON.stringify({"
        "  checks: (r['ledger-svc'].checks||[]).map(c=>c.cmd + ' ' + c.args.join(' ')),"
        "  nodeModulesDirs: r['ledger-svc'].nodeModulesDirs || null,"
        "  build: (d['ledger-svc'].build||[]).map(b=>b.cmd) }));"
    ) % (
        json.dumps(str(REPO_ROOT / "services" / "shared" / "projects-config.js")),
        json.dumps(str(wired["projects_file"])),
        json.dumps(str(wired["projects_file"])),
    )
    out = subprocess.run(["node", "-e", node_probe], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout)
    assert "go test ./..." in seen["checks"]
    assert seen["nodeModulesDirs"] is None, \
        "the reviewer's node_modules loops must tolerate a project that has none"
    assert seen["build"] == ["go"]


def test_provision_does_not_wait_for_the_codebase_map(sample_repo, wired, monkeypatch):
    """Reading a whole repository is minutes of model calls. Awaiting it here
    held the HTTP response open past the browser's own timeout, so the page
    reported a failure while the server went on and finished -- and the
    obvious next move, adding it again, is what put two projects on one
    repository. The map is started, not waited for."""
    _stub_side_effects(monkeypatch)
    started = []
    never_finishes = asyncio.Event()

    async def hangs(config, repo, store, force=False):
        started.append(repo)
        await never_finishes.wait()          # exactly what a long map looks like

    monkeypatch.setattr(srv.cartographer, "run_cartographer", hangs)
    client = TestClient(srv.app)

    report = client.post("/api/projects/detect", json={"path": str(sample_repo)}).json()
    res = client.post("/api/projects/provision", json={
        "path": report["live"],
        "choices": {
            "secret_files": [], "read_only_mounts": [], "pm2_apps": [],
            "node_modules_dirs": report["node_modules_dirs"],
            "checks": report["checks"], "build_steps": report["build_steps"],
            "db_env_file": report["db_env_file"],
        },
        "grant_access": False,
    })

    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert started == ["orders-api"], "the map must still be kicked off"
    step = {s["step"]: s for s in res.json()["steps"]}["codebase-map"]
    assert step["ok"] and "background" in step["detail"], \
        "and the operator must be told where it went"
    never_finishes.set()
