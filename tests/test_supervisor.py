"""The supervisor heals infrastructure escalations and nothing else.

The classification cases are the real escalation reasons of the thirty days
to 2026-09-23 (shas normalised): sixteen plumbing, three the task's own.
"""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from agent import supervisor as sv

HEAL = [
    ("review service did not review 37dd642d3182 within 900s", "review_timeout"),
    ("merge/deploy failed: {'ok': False, 'error': \"hint: Diverging branches can't be fast-forwarded\"}", "base_moved"),
    ("merge/deploy failed: {'ok': False, 'reason': 'stale', 'error': 'Newer commit(s) since the last review'}",
     "stale_review"),
    ("live moved on and the branch could not be rebased onto it: error: cannot rebase: You have unstaged changes.",
     "base_moved"),
    ("merge/deploy failed: {'ok': False, 'error': 'error: Your local changes to the following files would be "
     "overwritten by merge'}", "live_dirty"),
    ("merge/deploy failed: {'ok': False, 'stage': 'ship', 'error': 'could not push agent/x'}", "push_failed"),
    ("work node failed: peer closed connection without sending complete message body (incomplete chunked read)",
     "work_connection"),
    ("verify_and_ship failed: All connection attempts failed", "gate_connection"),
]
LEAVE = [
    "The independent review service escalated this after repeated non-converging rounds",
    "work node failed: Model call limits exceeded: run limit (400/400)",
    "work node failed: stuck in a tool loop: `bash` with identical arguments requested 12 times",
    "work node failed: [Errno 2] No such file or directory: '/x/services/llm-router/config.yaml'",
    "budget exhausted: $5.02 of $5.00",
    "hit max_iterations (40) without completing",
    "operator edit not applied: the task's branch moved after the editor was opened -- reopen it",
    "",
    None,
]


@pytest.mark.parametrize("reason,kind", HEAL)
def test_plumbing_is_recognised(reason, kind):
    assert sv.classify(reason).name == kind


@pytest.mark.parametrize("reason", LEAVE)
def test_the_tasks_own_failures_are_left_for_the_operator(reason):
    assert sv.classify(reason) is None


def test_a_cut_off_work_pass_resumes_work_and_a_gate_failure_does_not():
    assert sv.classify(HEAL[6][0]).stage == "work"
    assert all(sv.classify(r).stage == "gate" for r, k in HEAL if k != "work_connection")


# ── the sweep ────────────────────────────────────────────────────────────────

REASON = "review service did not review abc1234 within 900s"


class World:
    """A fake server: tasks, their checkpoints, and a record of every effect."""

    def __init__(self, *, reason=REASON, status="escalated", reviewer=True, landed=False, cap=3,
                 running=False, heal_meta=None, committed="abc1234", approved=None):
        self.meta = {"task_id": "t1", "status": status}
        if heal_meta:
            self.meta["heal"] = heal_meta
        self.values = {"repo": "p", "goal": "fix it", "budget_usd": 5.0, "escalated": status == "escalated",
                       "escalation_reason": reason if status == "escalated" else None,
                       "committed_sha": committed, "merge_approved_sha": approved,
                       "pending_merge_approval": {"sha": committed} if status == "awaiting_merge" else None}
        # Escalated moments before the first sweep in each test (they run
        # at now=0..20000), so the age limit is not what is being tested.
        self.values["execution_log"] = [{"timestamp": "1970-01-01T00:00:00Z"}]
        self.ckpt = "ck-1"
        self.reviewer, self.is_landed, self.cap, self.running = reviewer, landed, cap, running
        self.applied, self.meta_writes, self.alerts = [], [], []

    def deps(self):
        async def list_tasks(repo):
            return [dict(self.meta)]

        async def read_state(task_id):
            return self.values, self.ckpt

        async def apply(task_id, values, t, start):
            self.applied.append((t, start))
            return True

        async def write_meta(repo, task_id, **u):
            self.meta_writes.append(u)
            self.meta.update(u)

        async def up():
            return self.reviewer

        async def clean(live):
            return True

        async def landed(live, sha):
            return self.is_landed

        return sv.Deps(projects={"p": {"live": "/live", "sandbox": "/ws"}}, list_tasks=list_tasks,
                       read_state=read_state, is_running=lambda t: self.running, apply=apply,
                       write_meta=write_meta, notify=lambda *a: self.alerts.append(a),
                       reviewer_up=up, live_clean=clean, landed=landed, max_attempts=lambda: self.cap)


def _sweep(world, now):
    return asyncio.run(sv.sweep(world.deps(), now=now))


def test_it_waits_out_the_backoff_then_heals_through_the_gate():
    w = World()
    assert _sweep(w, now=1000) == []              # first sight: starts the clock
    assert w.applied == []
    assert _sweep(w, now=1030) == []              # inside the first backoff
    out = _sweep(w, now=1000 + sv.BACKOFF_S[0])
    assert out == [{"task": "t1", "action": "healed", "kind": "review_timeout", "attempt": 1}]
    t, start = w.applied[0]
    assert start is True and t.as_node == "work"   # next node: verify_and_ship, no model call
    assert t.patch["escalated"] is False and "pending_feedback" not in t.patch
    assert t.patch["execution_log"][0]["node"] == "supervisor"   # shows in the task's own stream
    assert w.meta["heal"]["attempts"] == 1
    assert w.alerts[0][0] == "auto_healed"


def test_it_does_not_heal_while_the_reviewer_is_still_down():
    w = World(reviewer=False)
    _sweep(w, now=0)
    assert _sweep(w, now=10_000) == [{"task": "t1", "action": "waiting", "kind": "review_timeout"}]
    assert w.applied == []
    assert w.meta["heal"].get("attempts", 0) == 0  # waiting is not an attempt


@pytest.mark.parametrize("reason", [r for r in LEAVE if r])
def test_it_never_touches_the_tasks_own_failures(reason):
    w = World(reason=reason)
    _sweep(w, now=0)
    assert _sweep(w, now=10_000) == [] and w.applied == []


def test_it_stops_at_the_cap_and_says_so_once():
    w = World(cap=2, heal_meta={"attempts": 2, "ckpt": "ck-0", "seen_at": 0})
    assert _sweep(w, now=10_000) == [{"task": "t1", "action": "gave_up", "kind": "review_timeout"}]
    assert _sweep(w, now=20_000) == []
    assert w.applied == []


def test_zero_attempts_means_off():
    w = World(cap=0)
    _sweep(w, now=0)
    _sweep(w, now=10_000)
    assert w.applied == []


def test_backoff_grows_with_each_heal():
    w = World(heal_meta={"attempts": 1, "ckpt": "ck-0", "seen_at": 0, "last_at": 0})
    _sweep(w, now=5000)                                    # a new escalation: clock restarts at 5000
    assert _sweep(w, now=5000 + sv.BACKOFF_S[0]) == []     # the first backoff is no longer enough
    assert _sweep(w, now=5000 + sv.BACKOFF_S[1])[0]["action"] == "healed"


def test_a_cut_off_work_pass_goes_back_to_work_with_a_note():
    w = World(reason=HEAL[6][0], approved="abc1234")
    _sweep(w, now=0)
    _sweep(w, now=10_000)
    t, _ = w.applied[0]
    assert t.as_node == "verify_and_ship" and "infrastructure failure" in t.patch["pending_feedback"]
    assert t.patch["merge_approved_sha"] is None


@pytest.mark.parametrize("status", ["escalated", "awaiting_merge"])
def test_work_already_on_main_is_concluded_not_retried(status):
    w = World(status=status, landed=True)
    assert _sweep(w, now=0) == [{"task": "t1", "action": "concluded"}]
    t, start = w.applied[0]
    assert start is False
    assert {"status": "done", "escalation_reason": None} in w.meta_writes
    assert w.alerts[0][0] == "auto_concluded"


def test_a_running_task_is_left_alone():
    w = World(running=True, landed=True)
    assert _sweep(w, now=10_000) == [] and w.applied == []


# ── "already on main", against real git ──────────────────────────────────────

def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "live"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@x")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("1\n")
    (r / "b.txt").write_text("1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _task_commit(repo, path, text):
    _git(repo, "checkout", "-qb", "agent/t")
    (repo / path).write_text(text)
    _git(repo, "commit", "-qam", "task")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def test_a_merged_commit_is_landed(repo):
    sha = _task_commit(repo, "a.txt", "2\n")
    _git(repo, "merge", "-q", "--ff-only", "agent/t")
    assert asyncio.run(sv.commit_landed(str(repo), sha))


def test_a_rebased_and_landed_commit_is_landed(repo):
    """What happened to four tasks on 2026-09-23: rebased, new shas, merged."""
    sha = _task_commit(repo, "a.txt", "2\n")
    (repo / "b.txt").write_text("main moved\n")
    _git(repo, "commit", "-qam", "someone else")
    _git(repo, "cherry-pick", sha)
    assert asyncio.run(sv.commit_landed(str(repo), sha))


def test_an_unmerged_commit_is_not(repo):
    sha = _task_commit(repo, "a.txt", "2\n")
    assert not asyncio.run(sv.commit_landed(str(repo), sha))


def test_a_commit_whose_change_main_did_differently_is_not(repo):
    """b43607c8's case: main solved the same problem another way. Its commit
    conflicts, so it has not landed, even though main moved on past it."""
    sha = _task_commit(repo, "a.txt", "2\n")
    (repo / "a.txt").write_text("3\n")
    _git(repo, "commit", "-qam", "main's own fix")
    assert not asyncio.run(sv.commit_landed(str(repo), sha))


def test_garbage_is_not_a_commit(repo):
    assert not asyncio.run(sv.commit_landed(str(repo), "HEAD; rm -rf /"))


# ── the server's half: a heal and an operator's click never both start ──────

def test_a_heal_loses_to_a_task_that_is_already_running(monkeypatch):
    import agent.server as server
    from agent import lifecycle

    class _Graph:
        updates = []

        async def aupdate_state(self, cfg, patch, **kw):
            self.updates.append(patch)

    monkeypatch.setattr(server.app.state, "graph", _Graph(), raising=False)
    started = []

    async def _stream(*a, **k):
        started.append(a)
    monkeypatch.setattr(server, "_stream_graph", _stream)
    t = lifecycle.Transition({"escalated": False}, "work")
    values = {"repo": "p", "goal": "g", "budget_usd": 1.0}

    async def go():
        deps = await server._supervisor_deps()
        server._running_tasks["busy"] = object()          # an operator's resume got there first
        try:
            lost = await deps.apply("busy", values, t, True)
        finally:
            server._running_tasks.pop("busy", None)
        won = await deps.apply("free", values, t, True)
        await asyncio.sleep(0)
        server._running_tasks.pop("free", None)
        return lost, won

    lost, won = asyncio.run(go())
    assert lost is False and won is True
    assert len(_Graph.updates) == 1 and len(started) == 1


def test_an_old_escalation_is_left_alone():
    """The first dry run against live data would have revived a task that had
    sat escalated for a month -- restarting old, paid work nobody asked for."""
    w = World(reason=HEAL[6][0])
    _sweep(w, now=0)
    assert _sweep(w, now=sv.MAX_AGE_S + 1) == [] and w.applied == []


def test_an_escalation_of_unknown_age_is_left_alone():
    w = World()
    w.values["execution_log"] = []
    _sweep(w, now=0)
    assert _sweep(w, now=10_000) == [] and w.applied == []


def test_old_work_already_on_main_is_still_concluded():
    """Closing is safe at any age; only reviving is limited."""
    w = World(landed=True)
    assert _sweep(w, now=sv.MAX_AGE_S * 30)[0]["action"] == "concluded"


# ── the whole project, not the newest 50 (2026-09-23 follow-up, F5) ─────────

class _PagedStore:
    """A store that pages like Postgres: offset honoured, newest first."""

    def __init__(self, metas):
        self.rows = [SimpleNamespace(namespace=("tasks", "proj"), key=m["task_id"], value=m,
                                     updated_at=None) for m in metas]

    async def asearch(self, ns, limit=10, offset=0, **kw):
        return self.rows[offset:offset + limit]


def _metas(n_live, parked_status="escalated"):
    """n_live running tasks, newest first, and ONE parked task older than all of them."""
    live = [{"task_id": f"live-{i:03d}", "status": "done"} for i in range(n_live)]
    return live + [{"task_id": "parked-old", "status": parked_status}]


def test_the_sweep_sees_a_parked_task_older_than_the_newest_fifty(monkeypatch):
    import agent.server as server
    monkeypatch.setattr(server.app.state, "store", _PagedStore(_metas(60)), raising=False)

    async def go():
        deps = await server._supervisor_deps()
        return await deps.list_tasks("proj")

    listed = asyncio.run(go())
    assert [m["task_id"] for m in listed] == ["parked-old"]   # and only what the sweep acts on


def test_startup_resume_and_the_inbox_see_the_whole_project_too(monkeypatch):
    import agent.server as server
    monkeypatch.setattr(server.app.state, "store", _PagedStore(_metas(150, "running")), raising=False)
    running = asyncio.run(server._tasks_in("proj", ("running", "queued")))
    assert [it.key for it in running] == ["parked-old"]
    # The inbox's "still being handled" set: a task outside any newest-N
    # window that is still running must not read as finished.
    assert "parked-old" in asyncio.run(server._github_live_tasks("proj"))
