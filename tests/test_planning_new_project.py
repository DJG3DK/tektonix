"""The planner proposes a new project; an admin confirms it from the chat.

Mid-conversation the operator describes a NEW application rather than a
change to the current project. The create_project tool records a proposal
(agent/tools/planning_tools.py); the turn persists it into the session meta
and streams it; POST /api/planning/sessions/{id}/new-project answers it.
Confirm runs the same _create_project as POST /api/projects/create and then
moves the session's two Store rows onto the new repo, so the conversation
continues there. Nothing here touches git or GitHub: _create_project is
stubbed, and the move is what is under test.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import planning_log
from agent.auth import User

_ADMIN = User(id=1, email="admin@example.com", role="admin", allowed_repos=None,
              totp_enabled=True, must_change_password=False,
              auto_approve_commands=False, require_merge_review=True)
_USER = User(id=2, email="op@example.com", role="user", allowed_repos=["shop"],
             totp_enabled=True, must_change_password=False,
             auto_approve_commands=False, require_merge_review=True)

PROPOSAL = {"name": "my-app", "description": "a store front", "github": True,
            "proposed_by": "admin@example.com"}


class _Item:
    def __init__(self, key, value):
        self.key, self.value = key, value


class _Store:
    """Namespace-aware: the session meta and its durable transcript live
    under different namespaces per repo, and the move is exactly a change of
    namespace, so a fake keyed on anything less could not see it."""

    def __init__(self):
        self.rows: dict[tuple, dict] = {}
        self.puts: list[tuple] = []

    async def aget(self, ns, key):
        rec = self.rows.get((tuple(ns), key))
        return _Item(key, rec) if rec is not None else None

    async def aput(self, ns, key, value):
        self.rows[(tuple(ns), key)] = dict(value)
        self.puts.append((tuple(ns), key))

    async def adelete(self, ns, key):
        self.rows.pop((tuple(ns), key), None)


def _seed(store: _Store, proposal: dict | None = PROPOSAL) -> None:
    store.rows[(("planning", "shop"), "s1")] = {
        "session_id": "s1", "repo": "shop", "title": "a new thing", "plan_markdown": "# Plan",
        "cost_usd": 0.5, "new_project": proposal, "updated_at": 1.0,
    }
    store.rows[((planning_log.NAMESPACE, "shop"), "s1")] = {
        "session_id": "s1", "entries": [{"kind": "user", "summary": "build me a store"}],
        "updated_at": 1.0,
    }


@pytest.fixture
def wired(monkeypatch):
    store = _Store()
    _seed(store)
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    # The real _find_planning_meta probes every repo in PROJECTS; the new
    # repo is "in PROJECTS" here the way reload_projects() would make it.
    monkeypatch.setattr(srv, "PROJECTS", {"shop": {"sandbox": "/tmp/shop"}, "my-app": {"sandbox": "/tmp/my-app"}})
    created: list = []

    async def _create(req, user):
        created.append((req, user))
        return {"ok": True, "name": req.name, "live": f"/srv/{req.name}",
                "steps": [{"step": "repository", "ok": True, "detail": "one commit"}],
                "github": None, "message": f"{req.name} is created."}

    monkeypatch.setattr(srv, "_create_project", _create)
    srv._running_planning_turns.pop("s1", None)
    return store, created


def _post(body: dict, session_id: str = "s1"):
    return TestClient(srv.app).post(f"/api/planning/sessions/{session_id}/new-project", json=body)


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def test_confirm_creates_through_the_shared_path_and_moves_the_session(wired):
    store, created = wired
    res = _post({"decision": "confirm"})
    assert res.status_code == 200, res.text
    body = res.json()

    # the same request shape POST /api/projects/create takes, built from the proposal
    assert len(created) == 1
    req, user = created[0]
    assert (req.name, req.description, req.github, req.token_name) == ("my-app", "a store front", True, None)
    assert user.email == "admin@example.com"

    assert body["project"]["ok"] is True
    assert body["session"]["repo"] == "my-app"
    assert body["session"]["new_project"] is None
    assert body["session"]["session_id"] == "s1"
    assert body["session"]["plan_markdown"] == "# Plan", "the move must carry the rest of the meta"

    # both rows live under the new repo now, and neither under the old one
    assert store.rows[(("planning", "my-app"), "s1")]["repo"] == "my-app"
    assert store.rows[((planning_log.NAMESPACE, "my-app"), "s1")]["entries"][0]["summary"] == "build me a store"
    assert (("planning", "shop"), "s1") not in store.rows
    assert ((planning_log.NAMESPACE, "shop"), "s1") not in store.rows


def test_the_moved_session_is_found_under_its_new_repo(wired):
    """The id routes carry no repo, so after the move every later call --
    the next message, the stream -- has to resolve the session to the new
    repo through the ordinary lookup."""
    import asyncio

    _post({"decision": "confirm"})
    repo, meta = asyncio.run(srv._find_planning_meta("s1"))
    assert repo == "my-app" and meta["repo"] == "my-app"


def test_the_card_can_override_the_github_choice(wired):
    _store, created = wired
    _post({"decision": "confirm", "github": False, "token_name": "work"})
    req, _ = created[0]
    assert req.github is False
    assert req.token_name == "work"


def test_a_failed_create_leaves_the_session_where_it_is(wired, monkeypatch):
    store, _ = wired

    async def _fail(req, user):
        return {"ok": False, "name": req.name, "live": f"/srv/{req.name}",
                "steps": [{"step": "github", "ok": False, "detail": "422: name taken"}], "github": None}

    monkeypatch.setattr(srv, "_create_project", _fail)
    res = _post({"decision": "confirm"})
    # 200, not an error: the UI renders the step list from the body
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project"]["ok"] is False
    assert body["project"]["steps"][0]["step"] == "github"
    assert body["session"]["repo"] == "shop"
    assert body["session"]["new_project"] == PROPOSAL, "the card stays up for a retry"
    assert (("planning", "shop"), "s1") in store.rows
    assert ((planning_log.NAMESPACE, "shop"), "s1") in store.rows
    assert (("planning", "my-app"), "s1") not in store.rows


def test_confirm_is_refused_while_a_turn_is_running(wired):
    store, created = wired
    srv._running_planning_turns["s1"] = object()
    try:
        res = _post({"decision": "confirm"})
        assert res.status_code == 409
        assert created == [], "a project was created under a running turn"
        assert store.rows[(("planning", "shop"), "s1")]["new_project"] == PROPOSAL
    finally:
        srv._running_planning_turns.pop("s1", None)


def test_a_refused_confirm_does_not_strand_the_run_slot(wired):
    """The claim must be released when the handler rejects, or the session
    could never take another message."""
    store, _ = wired
    store.rows[(("planning", "shop"), "s1")]["new_project"] = None
    res = _post({"decision": "confirm"})
    assert res.status_code == 409
    assert "s1" not in srv._running_planning_turns


def test_confirm_with_no_proposal_is_409(wired):
    store, created = wired
    store.rows[(("planning", "shop"), "s1")]["new_project"] = None
    res = _post({"decision": "confirm"})
    assert res.status_code == 409
    assert created == []


# ---------------------------------------------------------------------------
# dismiss, auth, lookup
# ---------------------------------------------------------------------------


def test_dismiss_clears_the_proposal_and_moves_nothing(wired):
    store, created = wired
    res = _post({"decision": "dismiss"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project"] is None
    assert body["session"]["new_project"] is None
    assert body["session"]["repo"] == "shop"
    assert store.rows[(("planning", "shop"), "s1")]["new_project"] is None
    assert created == []


def test_the_route_is_admin_only(wired, monkeypatch):
    store, created = wired
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _USER)
    for decision in ("confirm", "dismiss"):
        res = _post({"decision": decision})
        assert res.status_code == 403, res.text
    assert created == []
    assert store.rows[(("planning", "shop"), "s1")]["new_project"] == PROPOSAL


def test_unknown_session_is_404(wired):
    assert _post({"decision": "confirm"}, session_id="nope").status_code == 404


def test_an_unknown_decision_is_rejected_by_the_schema(wired):
    _store, created = wired
    assert _post({"decision": "maybe"}).status_code == 422
    assert created == []


# ---------------------------------------------------------------------------
# the turn persists what the tool recorded
# ---------------------------------------------------------------------------


class _Tracker:
    def __init__(self, total_cost: float):
        self.total_cost = total_cost


def _stub_turn(monkeypatch, store: _Store, *, proposes: dict | None):
    """_run_planning_turn_bg against a fake agent whose turn writes
    `proposes` into plan_ref the way the create_project tool does."""
    published: list[dict] = []
    seen: dict = {}

    async def _difficulty(_text, _config):
        return "EASY"

    async def _build(*_a, **kw):
        seen.update(kw)
        return object(), {"markdown": None, "brief": None}, _Tracker(0.1)

    async def _run(_agent, ref, _thread, _text, _publish, tracker=None):
        if proposes is not None:
            ref["new_project"] = proposes
        return None

    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setattr(srv.app.state, "checkpointer", object(), raising=False)
    monkeypatch.setattr(srv, "classify_planning_difficulty", _difficulty)
    monkeypatch.setattr(srv, "build_planning_agent", _build)
    monkeypatch.setattr(srv, "run_planning_turn", _run)
    monkeypatch.setattr(srv, "_publish_planning", lambda _sid, e: published.append(e))
    return published, seen


async def test_a_proposal_made_during_a_turn_is_persisted_and_streamed(monkeypatch):
    store = _Store()
    _seed(store, proposal=None)
    published, seen = _stub_turn(monkeypatch, store, proposes=PROPOSAL)
    await srv._run_planning_turn_bg("s1", "shop", "build me a store", is_admin=True, actor="admin@example.com")
    assert store.rows[(("planning", "shop"), "s1")]["new_project"] == PROPOSAL
    done = next(e for e in published if e["type"] == "turn_complete")
    assert done["new_project"] == PROPOSAL
    # role reaches the tool factory explicitly, not via allowed_repos
    assert seen["is_admin"] is True
    assert seen["actor"] == "admin@example.com"


async def test_a_turn_that_proposes_nothing_keeps_the_unanswered_proposal(monkeypatch):
    """PRESERVE, never clobber -- the same rule as the plan and the brief."""
    store = _Store()
    _seed(store, proposal=PROPOSAL)
    published, _ = _stub_turn(monkeypatch, store, proposes=None)
    await srv._run_planning_turn_bg("s1", "shop", "and add a cart")
    assert store.rows[(("planning", "shop"), "s1")]["new_project"] == PROPOSAL
    done = next(e for e in published if e["type"] == "turn_complete")
    assert done["new_project"] == PROPOSAL


async def test_role_defaults_off_when_the_caller_passes_none(monkeypatch):
    store = _Store()
    _seed(store, proposal=None)
    _published, seen = _stub_turn(monkeypatch, store, proposes=None)
    await srv._run_planning_turn_bg("s1", "shop", "hello")
    assert seen["is_admin"] is False
    assert seen["actor"] is None
