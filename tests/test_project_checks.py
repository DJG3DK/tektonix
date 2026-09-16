"""Post-merge check detection for a project that shipped with none
(agent/project_checks.py).

Two properties matter most here:

* The reviewer's answer decides, not projects.json's. Its built-ins win over
  projects.json key-by-key, so "has checks" can only be judged by asking it;
  and "cannot confirm" (unreachable, unknown project) must mean do nothing.
* The hook never raises. It runs inside verify_and_ship's outer try/except,
  which would turn any exception into an escalation of a task that has
  already shipped.
"""

import json
import subprocess
from pathlib import Path

import pytest

from agent import config as agent_config
from agent import project_checks as pc
from agent.provisioning import ProvisioningError


@pytest.fixture(autouse=True)
def _allow_tmp_as_project_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path / "workspaces"))


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


@pytest.fixture
def node_repo(tmp_path):
    repo = tmp_path / "shop-api"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({
        "name": "shop-api",
        "scripts": {"typecheck": "tsc --noEmit", "lint": "eslint src", "test": "vitest run"},
    }))
    (repo / "package-lock.json").write_text("{}")
    _git_init(repo)
    return repo


@pytest.fixture
def empty_repo(tmp_path):
    """git init + one commit and nothing else: exactly what the dashboard's
    "New project" produces before the first task."""
    repo = tmp_path / "blank"
    repo.mkdir()
    _git_init(repo)
    return repo


@pytest.fixture
def projects_file(tmp_path, monkeypatch):
    """A projects.json this test owns, wired as the one config.py reloads
    from. PROJECTS is restored afterwards so the shared dict does not leak
    a tmp project into the rest of the suite."""
    f = tmp_path / "projects.json"
    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", f)
    snapshot = dict(agent_config.PROJECTS)
    yield f
    agent_config.PROJECTS.clear()
    agent_config.PROJECTS.update(snapshot)


def _write(f: Path, projects: dict) -> None:
    f.write_text(json.dumps({"projects": projects}, indent=2) + "\n")


def _reviewer_says(monkeypatch, answer):
    seen = []

    async def _fake(force=False):
        seen.append(force)
        return answer

    monkeypatch.setattr(pc, "project_checks", _fake)
    return seen


# ---------------------------------------------------------------------------
# set_project_checks
# ---------------------------------------------------------------------------


def test_set_project_checks_is_atomic_and_leaves_other_keys_alone(tmp_path):
    p = tmp_path / "projects.json"
    _write(p, {
        "a": {"live": "/a", "sandbox": "/s/a", "db_env_file": ".env",
              "review": {"secretFiles": [".env"]}, "deploy": {"pm2Apps": ["a"]}},
        "b": {"live": "/b", "sandbox": "/s/b"},
    })
    pc.set_project_checks(p, "a", [{"name": "lint", "cmd": "npm", "args": ["run", "lint"]}])
    data = json.loads(p.read_text())["projects"]
    assert [c["name"] for c in data["a"]["review"]["checks"]] == ["lint"]
    assert data["a"]["review"]["secretFiles"] == [".env"], "sibling review keys survive"
    assert data["a"]["deploy"] == {"pm2Apps": ["a"]}
    assert data["a"]["db_env_file"] == ".env"
    assert data["b"] == {"live": "/b", "sandbox": "/s/b"}, "other projects untouched"
    assert not list(tmp_path.glob("*.tmp")), "temp file must be renamed away"


def test_set_project_checks_creates_the_review_section_when_absent(tmp_path):
    p = tmp_path / "projects.json"
    _write(p, {"a": {"live": "/a", "sandbox": "/s/a"}})
    pc.set_project_checks(p, "a", [{"name": "test", "cmd": "npm", "args": ["test"]}])
    assert json.loads(p.read_text())["projects"]["a"]["review"]["checks"][0]["name"] == "test"


def test_set_project_checks_refuses_to_overwrite_existing_checks(tmp_path):
    p = tmp_path / "projects.json"
    _write(p, {"a": {"live": "/a", "sandbox": "/s/a", "review": {"checks": [{"name": "keep"}]}}})
    with pytest.raises(ProvisioningError, match="already has 1 check"):
        pc.set_project_checks(p, "a", [{"name": "new"}])
    assert json.loads(p.read_text())["projects"]["a"]["review"]["checks"] == [{"name": "keep"}]


def test_set_project_checks_refuses_a_missing_entry_or_file(tmp_path):
    p = tmp_path / "projects.json"
    with pytest.raises(ProvisioningError):
        pc.set_project_checks(p, "a", [{"name": "x"}])
    _write(p, {"a": {"live": "/a", "sandbox": "/s/a"}})
    with pytest.raises(ProvisioningError, match="not in projects.json"):
        pc.set_project_checks(p, "ghost", [{"name": "x"}])
    with pytest.raises(ProvisioningError, match="no checks"):
        pc.set_project_checks(p, "a", [])


def test_reload_projects_sees_the_written_checks(projects_file):
    _write(projects_file, {"a": {"live": "/a", "sandbox": "/s/a"}})
    agent_config.reload_projects()
    assert "checks" not in agent_config.PROJECTS["a"].get("review", {})
    pc.set_project_checks(projects_file, "a", [{"name": "lint", "cmd": "npm", "args": ["run", "lint"]}])
    agent_config.reload_projects()
    assert agent_config.PROJECTS["a"]["review"]["checks"][0]["name"] == "lint"


# ---------------------------------------------------------------------------
# autodetect_checks_if_none
# ---------------------------------------------------------------------------


async def test_nothing_happens_when_the_reviewer_says_it_has_checks(monkeypatch, projects_file, node_repo):
    _write(projects_file, {"shop-api": {"live": str(node_repo), "sandbox": "/s/shop-api"}})
    agent_config.reload_projects()
    # projects.json has no checks, but the reviewer's built-ins do: the
    # reviewer's answer wins, nothing is written.
    seen = _reviewer_says(monkeypatch, {"shop-api": {"checks": 2, "names": ["lint", "test"]}})
    assert await pc.autodetect_checks_if_none("shop-api") is None
    assert seen == [True], "must bypass the 60s cache"
    assert "review" not in json.loads(projects_file.read_text())["projects"]["shop-api"]


async def test_nothing_happens_when_it_cannot_be_confirmed(monkeypatch, projects_file, node_repo):
    _write(projects_file, {"shop-api": {"live": str(node_repo), "sandbox": "/s/shop-api"}})
    agent_config.reload_projects()

    called = []
    monkeypatch.setattr(pc, "detect_project", lambda *a, **k: called.append(a))

    _reviewer_says(monkeypatch, {})  # unreachable
    assert await pc.autodetect_checks_if_none("shop-api") is None
    _reviewer_says(monkeypatch, {"other": {"checks": 0, "names": []}})  # unknown project
    assert await pc.autodetect_checks_if_none("shop-api") is None
    assert called == [], "detection must not run on a cannot-confirm"


async def test_recommended_checks_are_written_and_named_in_the_entry(monkeypatch, projects_file, node_repo):
    _write(projects_file, {"shop-api": {"live": str(node_repo), "sandbox": "/s/shop-api",
                                        "review": {"secretFiles": [".env"]}}})
    agent_config.reload_projects()
    _reviewer_says(monkeypatch, {"shop-api": {"checks": 0, "names": []}})

    entry = await pc.autodetect_checks_if_none("shop-api")

    assert entry is not None
    assert entry["node"] == "verify_and_ship" and entry["step_id"] is None and entry["cost_usd"] == 0.0
    written = json.loads(projects_file.read_text())["projects"]["shop-api"]
    names = [c["name"] for c in written["review"]["checks"]]
    assert {"typecheck", "lint", "test"} <= set(names)
    for name in names:
        assert name in entry["summary"]
    assert "npm" in entry["detail"] and "lint" in entry["detail"]
    assert written["review"]["secretFiles"] == [".env"]
    # Live in this process too, so the next task's gate sees them.
    assert agent_config.PROJECTS["shop-api"]["review"]["checks"] == written["review"]["checks"]
    assert not list(projects_file.parent.glob("*.tmp"))


async def test_a_second_run_after_the_write_is_a_no_op(monkeypatch, projects_file, node_repo):
    """The reviewer re-reads projects.json per poll, so once the checks are
    written it answers has-checks and the hook stands down."""
    _write(projects_file, {"shop-api": {"live": str(node_repo), "sandbox": "/s/shop-api"}})
    agent_config.reload_projects()
    _reviewer_says(monkeypatch, {"shop-api": {"checks": 0, "names": []}})
    assert await pc.autodetect_checks_if_none("shop-api") is not None
    _reviewer_says(monkeypatch, {"shop-api": {"checks": 3, "names": ["typecheck", "lint", "test"]}})
    assert await pc.autodetect_checks_if_none("shop-api") is None


async def test_an_empty_repo_gets_an_informational_entry_and_no_write(monkeypatch, projects_file, empty_repo):
    _write(projects_file, {"blank": {"live": str(empty_repo), "sandbox": "/s/blank"}})
    agent_config.reload_projects()
    _reviewer_says(monkeypatch, {"blank": {"checks": 0, "names": []}})

    entry = await pc.autodetect_checks_if_none("blank")

    assert entry is not None
    assert "none are detectable yet" in entry["summary"]
    assert "no recognized project manifest" in entry["detail"]
    assert "review" not in json.loads(projects_file.read_text())["projects"]["blank"]


async def test_a_blocker_is_informational_not_a_write(monkeypatch, projects_file, tmp_path):
    not_git = tmp_path / "plain"
    not_git.mkdir()
    (not_git / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    _write(projects_file, {"plain": {"live": str(not_git), "sandbox": "/s/plain"}})
    agent_config.reload_projects()
    _reviewer_says(monkeypatch, {"plain": {"checks": 0, "names": []}})

    entry = await pc.autodetect_checks_if_none("plain")

    assert entry is not None and "not a git repository" in entry["detail"]
    assert "review" not in json.loads(projects_file.read_text())["projects"]["plain"]


async def test_never_raises_when_detection_blows_up(monkeypatch, projects_file, node_repo, caplog):
    _write(projects_file, {"shop-api": {"live": str(node_repo), "sandbox": "/s/shop-api"}})
    agent_config.reload_projects()
    _reviewer_says(monkeypatch, {"shop-api": {"checks": 0, "names": []}})

    def _boom(*a, **k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(pc, "detect_project", _boom)
    assert await pc.autodetect_checks_if_none("shop-api") is None  # must not raise
    assert "post-merge check detection failed for shop-api" in caplog.text


async def test_never_raises_when_the_project_is_unknown_locally(monkeypatch):
    """The reviewer knows a project this process does not (built-in only):
    PROJECTS[repo] would KeyError, and that must stay inside the hook."""
    _reviewer_says(monkeypatch, {"builtin-only": {"checks": 0, "names": []}})
    assert await pc.autodetect_checks_if_none("builtin-only") is None
