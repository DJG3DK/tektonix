"""Planning spend is part of the task's cost (2026-09-28). A session's spend
is carried onto the task Build Now starts, once; Analytics counts it on the
task, and counts a plan that never became a task as its own category."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent import live_state, tasks
from agent.classify import TaskClassification


class _Item:
    def __init__(self, value):
        self.value = value


class _Store:
    def __init__(self, records):
        self.records = records   # {(ns, key): value}

    async def aget(self, ns, key):
        v = self.records.get((tuple(ns), key))
        return _Item(dict(v)) if v is not None else None

    async def aput(self, ns, key, value):
        self.records[(tuple(ns), key)] = dict(value)

    async def asearch(self, ns, limit=100):
        return [_Item(dict(v)) for (n, _k), v in self.records.items() if n == tuple(ns)]


def _app(store):
    async def stream(task_id, repo, goal, budget, state, **kw):
        return None

    async def classify(goal, config):
        return TaskClassification(category="feature", needs_tests=False)

    tasks.classify_task = classify
    return SimpleNamespace(state=SimpleNamespace(stream_graph=stream, config=object(), store=store))


def _start(app, session_id):
    async def go():
        out = await tasks.start_task(app, "build the thing", "proj", 3.0, "auto",
                                     auto_approve_commands=False, require_merge_review=True,
                                     planning_session_id=session_id)
        await live_state.running_tasks.pop(out["task_id"])
        return out["task_id"]
    return asyncio.run(go())


def test_the_sessions_spend_is_carried_onto_the_task_it_starts_once():
    store = _Store({(("planning", "proj"), "s1"): {"session_id": "s1", "repo": "proj", "cost_usd": 1.25}})
    app = _app(store)
    t1 = _start(app, "s1")
    meta = store.records[(("tasks", "proj"), t1)]
    assert meta["planning_cost_usd"] == 1.25 and meta["planning_session_id"] == "s1"
    sess = store.records[(("planning", "proj"), "s1")]
    assert sess["carried_cost_usd"] == 1.25 and sess["built_task_ids"] == [t1]
    # more planning, a second build: only the new spend is carried
    sess["cost_usd"] = 1.75
    t2 = _start(app, "s1")
    assert store.records[(("tasks", "proj"), t2)]["planning_cost_usd"] == 0.5
    assert store.records[(("planning", "proj"), "s1")]["carried_cost_usd"] == 1.75


def test_a_task_without_a_session_or_with_an_unknown_one_carries_nothing():
    store = _Store({})
    app = _app(store)
    t = _start(app, None)
    rec = store.records.get((("tasks", "proj"), t)) or {}
    assert "planning_cost_usd" not in rec, "no session, nothing carried, nothing written for it"
    t = _start(app, "ghost")
    assert store.records[(("tasks", "proj"), t)]["planning_cost_usd"] == 0.0


def test_analytics_counts_planning_on_the_task_and_stranded_plans_as_their_own_category(monkeypatch):
    import agent.server as srv
    from agent.auth import User
    from agent.routers import analytics as an

    store = _Store({
        (("tasks", "proj"), "t1"): {"task_id": "t1", "repo": "proj", "goal": "g", "category": "feature",
                                    "cost_so_far": 2.0, "planning_cost_usd": 0.5, "budget_usd": 5, "status": "done",
                                    "created_at": 1_790_000_000},
        (("planning", "proj"), "s1"): {"session_id": "s1", "repo": "proj", "cost_usd": 0.5, "carried_cost_usd": 0.5},
        (("planning", "proj"), "s2"): {"session_id": "s2", "repo": "proj", "cost_usd": 0.8},
    })
    monkeypatch.setattr(an, "PROJECTS", {"proj": {}})
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    me = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)
    body = TestClient(srv.app).get("/api/analytics").json()
    per_task = {t["task_id"]: t for t in body["per_task"]}
    assert per_task["t1"]["cost"] == 2.5 and per_task["t1"]["planning_cost"] == 0.5
    cats = {c["category"]: c for c in body["by_category"]}
    assert cats["feature"]["cost"] == 2.5, "the task's category carries the whole cost"
    assert cats["planning"] == {"category": "planning", "tasks": 1, "cost": 0.8}, "only the plan that never became a task"
