"""Several tasks on one project at once.

Each task has its own workspace (tests/test_task_workspaces.py). What remains
shared is pinned here: how many tasks a project runs at once (slots), the one
place they take turns (checks, review and merge), a review verdict per BRANCH
rather than per project, a reviewer that queues a request instead of dropping
it, and a sweep that frees finished tasks' workspaces without ever touching a
running one.
"""
import asyncio
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from agent import graph, paths, supervisor
from agent.tools.review_gate import branch_verdict, wait_for_review

REVIEWER = paths.REPO_ROOT / "services" / "commit-reviewer" / "reviewer.js"
A = "agent/11111111-2222-3333-4444-555555555555"
B = "agent/66666666-7777-8888-9999-000000000000"


# --- slots -------------------------------------------------------------------

async def _hold(repo, dsn, slots, entered, release, waits=None):
    async def on_wait():
        if waits is not None:
            waits.append(repo)
    async with graph.project_slot(repo, dsn, slots=slots, on_wait=on_wait) as index:
        entered.append(index)
        await release.wait()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_slots_let_that_many_tasks_in_and_queue_the_next(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "_SLOT_POLL_S", 0.05)
    dsn = None if backend == "memory" else f"sqlite:///{tmp_path / 'state.db'}"
    entered, waits, release = [], [], asyncio.Event()
    tasks = [asyncio.create_task(_hold(f"slots-{backend}", dsn, 2, entered, release, waits))
             for _ in range(3)]
    await asyncio.sleep(0.4)
    assert sorted(entered) == [0, 1], "two slots, two tasks in"
    assert waits == [f"slots-{backend}"], "the third waits, and says so once"
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert len(entered) == 3


async def test_postgres_slots_are_advisory_locks_under_distinct_keys(monkeypatch):
    """What production runs on. A fake Postgres that really tracks which
    advisory keys are held, across connections, the way the server does."""
    monkeypatch.setattr(graph, "_SLOT_POLL_S", 0.05)
    held: set = set()

    class _Cur:
        def __init__(self, row):
            self.row = row

        async def fetchone(self):
            return self.row

    class _Conn:
        async def execute(self, sql, params=None):
            key = tuple(params)
            if "pg_try_advisory_lock" in sql:
                free = key not in held
                held.add(key)
                return _Cur((free,))
            if "pg_advisory_unlock" in sql:
                held.discard(key)
            return _Cur((True,))

        async def close(self):
            return None

    async def _connect(dsn, autocommit=True):
        return _Conn()

    monkeypatch.setattr(graph, "_connect", _connect)
    entered, release = [], asyncio.Event()
    tasks = [asyncio.create_task(_hold("pg-repo", "postgresql://x/y", 2, entered, release))
             for _ in range(3)]
    await asyncio.sleep(0.4)
    assert sorted(entered) == [0, 1]
    assert (graph._LOCK_NAMESPACE, graph.advisory_key("pg-repo")) in held, "slot 0 is the old key"
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert held == set()


async def test_one_slot_is_the_old_project_lock_under_the_same_key():
    """So a process still on the single-slot code contends with this one."""
    held, release = asyncio.Event(), asyncio.Event()

    async def old_code():
        async with graph.project_lock("shared-key-repo", None):
            held.set()
            await release.wait()

    t = asyncio.create_task(old_code())
    await held.wait()
    got_in = asyncio.Event()

    async def new_code():
        async with graph.project_slot("shared-key-repo", None, slots=1):
            got_in.set()

    t2 = asyncio.create_task(new_code())
    await asyncio.sleep(0.2)
    assert not got_in.is_set()
    release.set()
    await asyncio.wait_for(asyncio.gather(t, t2), timeout=5)
    assert got_in.is_set()


async def test_the_ship_gate_is_one_at_a_time_whatever_the_slots(monkeypatch):
    from agent.nodes.verify_and_ship import _ship_gate

    order = []

    async def ship(name):
        async with _ship_gate("gate-repo", None):
            order.append(f"{name} in")
            await asyncio.sleep(0.1)
            order.append(f"{name} out")

    await asyncio.gather(ship("a"), ship("b"))
    assert order in (["a in", "a out", "b in", "b out"], ["b in", "b out", "a in", "a out"])


def test_the_setting_defaults_to_one_task_per_project():
    """Deploying this changes nothing until the operator raises it."""
    from agent import runtime_settings
    assert runtime_settings.KNOBS["parallel_tasks_per_project"]["default"] == 1


# --- the sweep ---------------------------------------------------------------

async def test_the_sweep_frees_finished_and_deleted_tasks_only():
    removed = []
    statuses = {"done-task": "done", "running-task": "running", "parked": "awaiting_merge",
                "escalated-task": "escalated", "stopped-task": "stopped"}

    async def remove(repo, name):
        removed.append(name)
        return {"ok": True, "removed": True}

    async def task_statuses(repo):
        return statuses

    deps = supervisor.Deps(
        projects={"p": {}}, list_tasks=None, read_state=None,
        is_running=lambda t: t == "running-but-unrecorded",
        apply=None, write_meta=None, notify=None, reviewer_up=None, live_clean=None,
        landed=None, max_attempts=lambda: 0,
        existing_workspaces=lambda repo: [*statuses, "deleted-task", "running-but-unrecorded"],
        remove_workspace=remove, task_statuses=task_statuses)
    out = await supervisor.sweep_workspaces(deps)
    assert sorted(removed) == ["deleted-task", "done-task"]
    assert {a["task"] for a in out} == {"deleted-task", "done-task"}


async def test_the_sweep_does_nothing_without_every_tasks_status():
    """list_tasks returns only PARKED tasks; reading it as 'the tasks that
    exist' would make every running task look deleted."""
    async def remove(repo, name):
        raise AssertionError("must not remove anything")

    deps = supervisor.Deps(
        projects={"p": {}}, list_tasks=None, read_state=None, is_running=lambda t: False,
        apply=None, write_meta=None, notify=None, reviewer_up=None, live_clean=None,
        landed=None, max_attempts=lambda: 0,
        existing_workspaces=lambda repo: ["some-task"], remove_workspace=remove, task_statuses=None)
    assert await supervisor.sweep_workspaces(deps) == []


# --- a verdict per branch ----------------------------------------------------

def test_a_branch_reads_its_own_verdict_not_the_latest():
    project = {"branch": B, "lastReviewedSha": "b1", "verdict": "NEEDS_FIXES",
               "branches": {A: {"branch": A, "lastReviewedSha": "a1", "verdict": "READY"},
                            B: {"branch": B, "lastReviewedSha": "b1", "verdict": "NEEDS_FIXES"}}}
    assert branch_verdict(project, A)["verdict"] == "READY"
    assert branch_verdict(project, B)["verdict"] == "NEEDS_FIXES"
    assert branch_verdict(project, None) is project


def test_a_record_from_before_per_branch_verdicts_is_its_branchs_only():
    legacy = {"branch": A, "lastReviewedSha": "a1", "verdict": "READY"}
    assert branch_verdict(legacy, A)["lastReviewedSha"] == "a1"
    assert branch_verdict(legacy, B) is None


async def test_waiting_for_a_review_reads_the_tasks_own_branch(monkeypatch):
    async def fake_state(project, branch=None):
        latest = {"branch": B, "lastReviewedSha": "b1", "verdict": "READY",
                  "branches": {A: {"lastReviewedSha": "a1", "verdict": "READY"}}}
        return branch_verdict(latest, branch)

    monkeypatch.setattr("agent.tools.review_gate._read_state", fake_state)
    got = await wait_for_review("p", "a1", timeout=1, poll_interval=0.01, branch=A)
    assert got["verdict"] == "READY"


# --- the reviewer's side -----------------------------------------------------

def _node(expr: str, state_dir) -> object:
    out = subprocess.run(
        ["node", "-e", f"const r=require({json.dumps(str(REVIEWER))});"
                       f"Promise.resolve({expr}).then((v)=>console.log(JSON.stringify(v === undefined ? null : v)))"],
        capture_output=True, text=True, timeout=60, cwd=str(REVIEWER.parent),
        env={**os.environ, "REVIEW_STATE_DIR": str(state_dir), "MODEL_ROUTER_KEY": "unused"})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_reviewer_keeps_a_record_per_branch_and_prunes_the_oldest(tmp_path):
    rec = _node(f"r.branchRecord({{branch:{json.dumps(A)}, lastReviewedSha:'a1', branches:{{}}}}, {json.dumps(A)})",
                tmp_path)
    assert rec["lastReviewedSha"] == "a1" and "branches" not in rec
    assert _node(f"r.branchRecord({{branch:{json.dumps(A)}, lastReviewedSha:'a1'}}, {json.dumps(B)})",
                 tmp_path) is None
    many = "Object.fromEntries(Array.from({length:45},(_,i)=>['agent/x'+i,{reviewedAt:String(1000+i)}]))"
    kept = _node(f"Object.keys(r.withBranchRecord({{branches:{many}}}, 'agent/new', {{reviewedAt:'9999'}}))",
                 tmp_path)
    assert len(kept) == 40 and "agent/new" in kept and "agent/x0" not in kept


def test_churn_is_counted_per_branch(tmp_path):
    """Two tasks each drawing a finding on the same file is not one task
    going round in circles."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    with open(tmp_path / "history.jsonl", "w") as fh:
        for branch in (A, B, B):
            fh.write(json.dumps({"project": "p", "branch": branch, "reviewedAt": now,
                                 "findings": [{"file": "src/x.js"}]}) + "\n")
    finding = "[{file:'src/x.js'}]"
    assert _node(f"r.computeFileChurn('p', {finding}, {json.dumps(A)})", tmp_path) is None
    assert _node(f"r.computeFileChurn('p', {finding}, {json.dumps(B)})", tmp_path) is not None


def test_a_request_to_a_busy_reviewer_is_queued_once(tmp_path):
    got = _node(f"(r.queueReview('p',{json.dumps(A)}), r.queueReview('p',{json.dumps(B)}),"
                f" r.queueReview('p',{json.dumps(A)}), r.pendingReviews.get('p'))", tmp_path)
    assert got == [A, B]


# --- merging one branch leaves the others' verdicts ------------------------

SECRET = "test-control-secret-0123456789abcdef"


def _deps_installed() -> bool:
    return subprocess.run(["node", "-e", "require.resolve('express')"],
                          cwd=str(paths.REPO_ROOT / "services" / "agent-review"),
                          capture_output=True).returncode == 0


def _git(args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          env=env).stdout.strip()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Review-Secret": SECRET})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.skipif(not _deps_installed() and os.environ.get("REQUIRE_SERVICE_TESTS") != "1",
                    reason="services/agent-review dependencies not installed")
def test_the_merge_takes_the_named_branch_and_clears_only_its_verdict(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    _git(["init", "-q", "-b", "main"], live)
    (live / "f").write_text("base\n")
    _git(["add", "-A"], live)
    _git(["commit", "-qm", "base"], live)
    tips = {}
    for branch, content in ((A, "a\n"), (B, "b\n")):
        _git(["checkout", "-q", "-b", branch, "main"], live)
        (live / branch.split("/")[1]).write_text(content)
        _git(["add", "-A"], live)
        _git(["commit", "-qm", branch], live)
        tips[branch] = _git(["rev-parse", "HEAD"], live)
    _git(["checkout", "-q", "main"], live)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    def rec(b):
        return {"branch": b, "lastReviewedSha": tips[b], "verdict": "READY", "reviewedAt": "x"}

    # B was reviewed last, so it is the project's latest -- A is the one merging.
    (state_dir / "state.json").write_text(json.dumps(
        {"p": {**rec(B), "branches": {A: rec(A), B: rec(B)}}}))
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps({"projects": {"p": {"live": str(live), "sandbox": str(live)}}}))

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["node", str(paths.REPO_ROOT / "services/agent-review/server.js")],
        env={**os.environ, "AGENT_PROJECTS_JSON": str(projects), "REVIEW_ONLY_PROJECTS_JSON": "1",
             "REVIEW_STATE_DIR": str(state_dir), "REVIEW_CONTROL_SECRET": SECRET,
             "MODEL_ROUTER_KEY": "unused", "REVIEW_BIND_ADDRESS": "127.0.0.1",
             "REVIEW_SERVICE_PORT": str(port)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except urllib.error.HTTPError:
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        status, body = _post(f"http://127.0.0.1:{port}/api/projects/p/merge", {"branch": A})
        assert status == 200 and body["ok"], body
        assert _git(["rev-parse", "main"], live) == tips[A]
        state = json.loads((state_dir / "state.json").read_text())["p"]
        assert A not in state["branches"] and state["branches"][B]["verdict"] == "READY"
        # A name that is not a task branch is refused, not guessed at.
        status, body = _post(f"http://127.0.0.1:{port}/api/projects/p/merge", {"branch": "main"})
        assert status == 400
    finally:
        proc.kill()
        proc.wait()
