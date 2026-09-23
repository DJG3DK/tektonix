"""The properties that stop an eval run from touching production.

Each of these is a switch that, if it silently stopped working, would not
break a run -- it would corrupt something else while the run reported
success. That is the worst shape a bug can have in a measurement tool, so the
switches are pinned here rather than left to the comments that explain them.

The Node-side ones are asserted by running node against the real service
files, because the property belongs to those files and a Python re-statement
of it would drift the first time somebody edited the JavaScript.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from agent import paths

REVIEWER_JS = paths.REPO_ROOT / "services" / "commit-reviewer" / "reviewer.js"
DASHBOARD_JS = paths.REPO_ROOT / "services" / "agent-review" / "server.js"
SHARED = paths.REPO_ROOT / "services" / "shared" / "projects-config.js"


def _node(script: str, env: dict | None = None, cwd: Path | None = None) -> str:
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          env={**os.environ, **(env or {})},
                          cwd=str(cwd or SHARED.parent), timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def eval_projects_json(tmp_path):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"projects": {
        "pylib": {"live": str(tmp_path / "live"), "sandbox": str(tmp_path / "sandbox"),
                  "review": {"checks": [{"name": "test", "cmd": "true", "args": []}]}},
    }}))
    return path


# --- the Node side ---------------------------------------------------------

def test_projects_json_is_read_from_the_env_var(eval_projects_json):
    """AGENT_PROJECTS_JSON is what points a second reviewer at the fixtures
    instead of the operator's real repositories."""
    out = _node(
        "const {readProjectsJson} = require('./projects-config');"
        "console.log(JSON.stringify(Object.keys(readProjectsJson())))",
        env={"AGENT_PROJECTS_JSON": str(eval_projects_json)})
    assert json.loads(out) == ["pylib"]


def test_the_review_section_is_what_a_fixture_supplies_checks_through(eval_projects_json):
    """A fixture goes through the reviewer as an ordinary project. If this
    stopped reading the `review` block, every eval task would be reviewed with
    no checks at all -- and would sail through."""
    out = _node(
        "const {loadProjects} = require('./projects-config');"
        "const p = loadProjects({}, {section: 'review'});"
        "console.log(JSON.stringify(p.pylib.checks.map(c => c.name)))",
        env={"AGENT_PROJECTS_JSON": str(eval_projects_json)})
    assert json.loads(out) == ["test"]


def test_builtins_are_skipped_when_only_projects_json_is_asked_for(tmp_path, eval_projects_json):
    """The hole the first smoke run of agent/evals/reviewer.py found.

    Pointing AGENT_PROJECTS_JSON at the fixtures is not enough on its own: a
    built-in-only project appears whether or not projects.json mentions it, so
    the eval instance came up polling real repositories. With a live task
    branch on one of them, two reviewers would be racing on the same repo.
    """
    builtin = tmp_path / "builtin-projects.local.js"
    builtin.write_text("module.exports = {'a-real-project': {live: '/somewhere'}};\n")
    probe = (
        "const path = require('path');"
        "let builtins = {};"
        "if (process.env.REVIEW_ONLY_PROJECTS_JSON !== '1') {"
        "  builtins = require(process.env.BUILTIN_PATH);"
        "}"
        f"const {{loadProjects}} = require('{SHARED}');"
        "console.log(JSON.stringify(Object.keys(loadProjects(builtins, {section: 'review'})).sort()))"
    )
    env = {"AGENT_PROJECTS_JSON": str(eval_projects_json), "BUILTIN_PATH": str(builtin)}
    with_builtins = json.loads(_node(probe, env=env))
    without = json.loads(_node(probe, env={**env, "REVIEW_ONLY_PROJECTS_JSON": "1"}))
    assert with_builtins == ["a-real-project", "pylib"]
    assert without == ["pylib"], "an eval reviewer must see the fixtures and nothing else"


def test_both_services_honour_the_only_projects_json_switch():
    """Asserted against the real files: the property belongs to them, and a
    guard added to one and not the other is exactly the half-fix that reads
    as done."""
    for js in (REVIEWER_JS, DASHBOARD_JS):
        source = js.read_text()
        assert "REVIEW_ONLY_PROJECTS_JSON" in source, f"{js.name} does not honour the switch"
        # The require must be INSIDE the guard, not merely near it.
        guarded = re.search(
            r"if \(process\.env\.REVIEW_ONLY_PROJECTS_JSON !== '1'\) \{[^}]*"
            r"require\('\./builtin-projects\.local'\)", source, re.S)
        assert guarded, f"{js.name} guards the flag but still requires the built-ins"


def test_both_services_take_their_ports_from_the_environment():
    """Two reviewer pairs must be able to run at once -- an eval while the
    live one is mid-review."""
    assert "process.env.REVIEW_CONTROL_PORT" in REVIEWER_JS.read_text()
    assert "process.env.REVIEW_SERVICE_PORT" in DASHBOARD_JS.read_text()


def test_the_reviewers_mutable_files_are_all_overridable():
    """usage.jsonl is the one that matters: the dashboard's reviewer-spend
    figure is summed from it, so a second instance writing there silently
    inflates the number the eval exists to explain."""
    source = REVIEWER_JS.read_text()
    for var in ("REVIEW_STATE_DIR", "REVIEW_USAGE_LOG", "REVIEW_WORKTREE_ROOT"):
        assert var in source, f"{var} is not overridable, so an eval run would share it"


# --- the Python side -------------------------------------------------------

def test_the_agent_reads_both_review_ports_from_the_environment(monkeypatch):
    import importlib

    import agent.tools.review_gate as rg
    monkeypatch.setenv("REVIEW_CONTROL_PORT", "14101")
    monkeypatch.setenv("REVIEW_SERVICE_PORT", "14100")
    importlib.reload(rg)
    try:
        assert (rg.REVIEW_CONTROL_PORT, rg.REVIEW_SERVICE_PORT) == (14101, 14100)
    finally:
        monkeypatch.undo()
        importlib.reload(rg)
    assert (rg.REVIEW_CONTROL_PORT, rg.REVIEW_SERVICE_PORT) == (4101, 4100)


@pytest.mark.parametrize("bad", ["nope", "0", "99999", "-1"])
def test_a_nonsense_port_is_refused_rather_than_silently_defaulted(monkeypatch, bad):
    """Falling back to 4101 on a bad value would send an eval's commits to the
    LIVE reviewer, which is the failure this whole module exists to prevent --
    and it would do it quietly."""
    import importlib

    import agent.tools.review_gate as rg
    monkeypatch.setenv("REVIEW_CONTROL_PORT", bad)
    with pytest.raises(RuntimeError, match="REVIEW_CONTROL_PORT"):
        importlib.reload(rg)
    monkeypatch.undo()
    importlib.reload(rg)


def test_the_eval_reviewer_env_isolates_every_shared_file(tmp_path):
    from agent.evals.reviewer import _child_env

    env = _child_env(tmp_path / "projects.json", tmp_path / "state", 1234, 5678)
    assert env["REVIEW_ONLY_PROJECTS_JSON"] == "1"
    assert env["AGENT_PROJECTS_JSON"] == str(tmp_path / "projects.json")
    # Not merely set -- set to somewhere under the run's own directory.
    for var in ("REVIEW_STATE_DIR", "REVIEW_USAGE_LOG", "REVIEW_WORKTREE_ROOT"):
        assert str(tmp_path) in env[var], f"{var} still points outside the run"
    assert env["REVIEW_BIND_ADDRESS"] == "127.0.0.1"


def test_the_eval_store_can_never_be_the_production_one(tmp_path):
    """An eval writes an episode per task, and episodes are what the Analytics
    panel is computed from."""
    import dataclasses

    from agent.evals.runner import eval_config

    @dataclasses.dataclass(frozen=True)
    class Cfg:
        dsn: str

    for live in ("postgresql://u:p@host/agent", "postgres://localhost/agent"):
        assert eval_config(Cfg(dsn=live), tmp_path).dsn.startswith("sqlite:///")


def test_the_entry_script_sets_the_env_before_importing_the_agent():
    """The import order in scripts/run_evals.py is load-bearing: both port
    variables and AGENT_PROJECTS_JSON are frozen into module state at import,
    and setting them late fails silently and wrongly."""
    source = (paths.REPO_ROOT / "scripts" / "run_evals.py").read_text()
    set_projects = source.index('os.environ["AGENT_PROJECTS_JSON"]')
    set_ports = source.index("os.environ.update(rev.env_overrides)")
    import_agent = source.index("from agent.outer_graph import build_outer_graph")
    assert set_projects < import_agent
    assert set_ports < import_agent


# --- the run is configured like production --------------------------------

def _run_evals_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_evals_under_test", paths.REPO_ROOT / "scripts" / "run_evals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_the_run_takes_productions_runtime_settings(monkeypatch):
    """They live in the task store and the run's store is a fresh file, so a
    run used the built-in defaults: a 180s model-call timeout against
    production's 300s, which failed a task production would have finished."""
    import contextlib
    from types import SimpleNamespace

    from agent import runtime_settings as rs

    monkeypatch.setattr(rs, "_values", dict(rs._values))
    stored = {"model_call_timeout_s": 300.0}

    class Store:
        async def aget(self, ns, key):
            return SimpleNamespace(value=stored)

    @contextlib.asynccontextmanager
    async def open_store(_cfg):
        yield Store()

    note = await _run_evals_module().live_runtime_settings(object(), open_store)
    assert rs.value("model_call_timeout_s") == 300.0
    assert "model_call_timeout_s=300" in note


@pytest.mark.asyncio
async def test_no_live_store_leaves_the_defaults_and_says_so(monkeypatch):
    import contextlib

    from agent import runtime_settings as rs

    monkeypatch.setattr(rs, "_values", dict(rs._values))
    before = rs.all_values()

    @contextlib.asynccontextmanager
    async def open_store(_cfg):
        raise ConnectionError("no database here")
        yield

    note = await _run_evals_module().live_runtime_settings(object(), open_store)
    assert note.startswith("defaults")
    assert rs.all_values() == before
