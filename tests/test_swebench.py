"""SWE-bench, run exactly as SWE-bench (agent/evals/swebench.py, evals/SWEBENCH.md).

The rules that make a result advertisable as a SWE-bench result are pinned here:
the official image name, the issue and nothing else in the goal, no network and
no outside tools in a benchmark project, and a prediction that is the agent's
source change -- not its tests, not the image's build output.
"""
import subprocess

import pytest

from agent.config import PROJECTS
from agent.evals import swebench as sb


def test_the_image_is_the_harness_s_own_name_for_the_task():
    assert sb.image_name("psf__requests-1142") == "swebench/sweb.eval.x86_64.psf_1776_requests-1142:latest"
    assert sb.image_name("Django__Django-10097").islower()


def test_the_goal_is_the_issue_and_nothing_that_grades_it():
    inst = {"repo": "psf/requests", "problem_statement": "  Something is broken.\n",
            "hints_text": "THE HINT", "FAIL_TO_PASS": '["tests/test_x.py::test_secret"]',
            "patch": "THE FIX", "test_patch": "THE TESTS"}
    goal = sb.goal_for(inst)
    assert "<issue>\nSomething is broken.\n</issue>" in goal
    for leak in ("THE HINT", "test_secret", "THE FIX", "THE TESTS"):
        assert leak not in goal


def test_a_sample_is_seeded_and_ids_must_exist():
    rows = [{"instance_id": f"r__r-{i}"} for i in range(40)]
    assert sb.select(rows, sample=5, seed=3) == sb.select(rows, sample=5, seed=3)
    assert sb.select(rows, sample=5, seed=3) != sb.select(rows, sample=5, seed=4)
    with pytest.raises(sb.SetupError):
        sb.select(rows, ids=["nope__nope-1"])


@pytest.mark.parametrize("path,is_test", [
    ("tests/test_models.py", True), ("testing/python/raises.py", True), ("requests/test_utils.py", True),
    ("sklearn/utils/_testing.py", False), ("django/test/client.py", True), ("conftest.py", True),
    ("astropy/io/fits/card.py", False), ("src/_pytest/python.py", False), ("sympy/core/basic.py", False),
])
def test_what_counts_as_a_test_file(path, is_test):
    assert sb.is_test_path(path) is is_test


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


def test_the_prediction_is_the_source_change_only(tmp_path, monkeypatch):
    """Not test files (the harness applies the task's own test patch after it,
    and a clash fails the task), not the image's build output, not caches."""
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}.items():
        monkeypatch.setenv(k, v)
    live = tmp_path / "live"
    (live / "pkg").mkdir(parents=True)
    (live / "tests").mkdir()
    (live / "pkg" / "core.py").write_text("x = 1\n")
    (live / "tests" / "test_core.py").write_text("def test(): pass\n")
    _git(["init", "-q", "-b", "main"], live)
    _git(["add", "-A"], live)
    _git(["commit", "-qm", "SWE-bench"], live)
    (live / "build").mkdir()
    (live / "build" / "lib.txt").write_text("built\n")                 # present before the task
    base = _git(["rev-parse", "HEAD"], live).strip()
    ws = tmp_path / "ws"
    _git(["worktree", "add", "-q", "--detach", str(ws), base], live)
    (ws / "build").mkdir()
    (ws / "build" / "lib.txt").write_text("built\n")
    (ws / "pkg" / "core.py").write_text("x = 2\n")                    # committed below
    _git(["commit", "-qam", "fix"], ws)
    (ws / "pkg" / "new.py").write_text("y = 1\n")                     # created, uncommitted
    (ws / "tests" / "test_core.py").write_text("def test(): assert 0\n")
    (ws / "pkg" / "__pycache__").mkdir()
    (ws / "pkg" / "__pycache__" / "core.cpython-39.pyc").write_bytes(b"\0")
    patch = sb.prediction_patch(ws, base, live)
    assert "pkg/core.py" in patch and "+x = 2" in patch
    assert "pkg/new.py" in patch and "+y = 1" in patch
    for absent in ("tests/test_core.py", "build/lib.txt", "__pycache__"):
        assert absent not in patch


def test_a_benchmark_project_runs_offline_in_its_image_with_its_own_gate(monkeypatch):
    from agent.tools.sandbox import sandbox_environment_for, sandbox_layout_for
    inst = {"repo": "sympy/sympy"}
    entry = sb.project_entry(inst, {"live": "/l", "sandbox": "/tmp/sbx-proj", "image": "swebench/x:latest",
                                    "base": "abc123"})
    monkeypatch.setitem(PROJECTS, "sbx-proj", entry)
    assert sandbox_environment_for("/tmp/sbx-proj")[0] == "swebench/x:latest"
    layout = sandbox_layout_for("/tmp/sbx-proj")
    assert layout == {"mounts": ["/testbed"], "init": sb.CONDA_INIT, "network": "none"}
    names = [c["name"] for c in entry["checks"]]
    assert names == ["compile", "import"] and "import sympy" in entry["checks"][1]["cmd"]
    assert entry["benchmark"] is True and entry["review"] == {"checks": []}


def test_a_project_cannot_mount_over_the_container_s_own_system():
    from agent.tools import sandbox as sbx
    PROJECTS["sbx-bad"] = {"sandbox": "/tmp/sbx-bad", "sandbox_mounts": ["/etc", "/usr", "../x", "/testbed", "relative"],
                           "sandbox_network": "host"}
    try:
        layout = sbx.sandbox_layout_for("/tmp/sbx-bad")
        assert layout["mounts"] == ["/testbed"]
        assert layout["network"] is None, "only none or bridge; never the host's network"
    finally:
        PROJECTS.pop("sbx-bad", None)


async def test_a_project_s_declared_checks_replace_the_npm_scripts(monkeypatch, tmp_path):
    from agent.tools import checks
    ran = []

    async def fake(cmd, cwd, timeout=None, network=None, **k):
        ran.append((cmd, network))
        return {"ok": "fail" not in cmd, "exit_code": 0, "output": "out"}

    monkeypatch.setattr(checks, "run_shell_sandboxed", fake)
    monkeypatch.setitem(PROJECTS, "declared", {"sandbox": str(tmp_path), "checks": [
        {"name": "compile", "cmd": "python -m py_compile x.py"}, {"name": "import", "cmd": "fail please"}]})
    out = await checks.run_all_checks(str(tmp_path), "declared")
    assert ran == [("python -m py_compile x.py", "none"), ("fail please", "none")]
    assert out["all_ok"] is False and set(out["checks"]) == {"compile", "import"}


def test_a_benchmark_seat_has_no_way_out_of_its_repository():
    """The published fix is on GitHub. No GitHub tools, no web pages, no other
    projects -- and (above) no network in its shell."""
    import inspect

    from agent import deep_agent
    src = inspect.getsource(deep_agent.build_deep_agent)
    for guarded in ("github_tools = [] if benchmark", "visual_tools = [] if benchmark",
                    "reference_tools = [] if benchmark"):
        assert guarded in src


# --- batches: a full run never fills the disk ---------------------------------

def _runner():
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("run_swebench", pathlib.Path("scripts/run_swebench.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_each_batch_is_graded_then_its_images_deleted_and_the_disk_stops_the_run(monkeypatch, tmp_path):
    """500 images are over a terabyte. A batch is run, graded while its images
    are still here, then its images go; a disk past the limit stops the run
    before the next batch rather than filling it. One official report covers
    every batch that ran, and what did not run says why."""
    import json
    mod = _runner()
    rows = [{"instance_id": f"r__r-{i}", "repo": "r/r", "image": f"img-{i}"} for i in range(5)]
    monkeypatch.setattr(mod, "RUNS", tmp_path)
    monkeypatch.setattr(mod.sb, "load_instances", lambda: rows)
    events, spent_seen = [], []
    # Two batches' worth of images fill the disk past the limit.
    monkeypatch.setattr(mod, "disk_used_pct", lambda path="/": 95 if len(spent_seen) >= 2 else 10)

    async def fake_run(args, batch, root, run_dir, spent_before=0.0):
        spent_seen.append(spent_before)
        events.append(("run", [i["instance_id"] for i in batch]))
        return {"results": {i["instance_id"]: {"outcome": "done", "cost_usd": 1.0} for i in batch},
                "runtime_settings": {"x": 1}}

    def fake_grade(pred, ids, run_id, report_dir, max_workers=4, rewrite=False, **k):
        events.append(("rewrite" if rewrite else "grade", list(ids)))
        return {"resolved_ids": [i for i in ids if i.endswith(("-0", "-3"))]}

    def fake_subprocess_run(cmd, **k):
        if cmd[:3] == ["docker", "image", "rm"]:
            events.append(("rmi", cmd[-1]))
    monkeypatch.setattr(mod, "_run", fake_run)
    monkeypatch.setattr(mod.sb, "grade", fake_grade)
    monkeypatch.setattr(mod.subprocess, "run", fake_subprocess_run)

    assert mod.main(["--all", "--batch-size", "2", "--run-id", "t", "--max-disk-pct", "80"]) == 0
    assert events == [
        ("run", ["r__r-0", "r__r-1"]), ("grade", ["r__r-0", "r__r-1"]), ("rmi", "img-0"), ("rmi", "img-1"),
        ("run", ["r__r-2", "r__r-3"]), ("grade", ["r__r-2", "r__r-3"]), ("rmi", "img-2"), ("rmi", "img-3"),
        ("rewrite", ["r__r-0", "r__r-1", "r__r-2", "r__r-3"]),
    ]
    assert spent_seen == [0.0, 2.0], "the ceiling counts the whole run, not one batch"
    summary = json.loads((tmp_path / "t" / "summary.json").read_text())
    assert summary["resolved"] == 2 and summary["total"] == 5 and summary["resolved_rate"] == 40.0
    assert "95%" in summary["stopped_early"]
    assert summary["instances"]["r__r-4"] == {"outcome": "not_run", "resolved": False}


def test_a_trajectory_is_every_message_each_conversation_held():
    """The agent's `messages` is a DeltaChannel: its checkpoint holds only a
    marker, and the first runs saved an empty trajectory for every task. The
    conversation is rebuilt from the writes, including what summarization
    later removed, each subagent's separately."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
    mod = _runner()

    def tup(cid, ns, writes):
        return SimpleNamespace(config={"configurable": {"checkpoint_id": cid, "checkpoint_ns": ns}},
                               checkpoint={"channel_values": {}}, pending_writes=writes)
    goal, call = HumanMessage("fix it", id="h"), AIMessage("", id="a", tool_calls=[{"name": "read", "args": {}, "id": "c"}])
    out = ToolMessage("file", tool_call_id="c", id="t")
    sub = AIMessage("delegated work", id="s")
    tuples = [
        tup("3", "", [("x", "messages", [RemoveMessage(id="h")]), ("x", "todos", [])]),   # summarization
        tup("1", "", [("x", "messages", [goal])]),
        tup("2", "", [("x", "messages", [call, out]), ("x", "messages", [call])]),
        tup("2", "tools:abc", [("y", "messages", sub)]),
    ]
    convs = mod.conversations(tuples)
    assert [m.id for m in convs[""]] == ["h", "a", "t"]
    assert [m.id for m in convs["tools:abc"]] == ["s"]
