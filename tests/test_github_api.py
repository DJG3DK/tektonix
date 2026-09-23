"""The GitHub endpoints: settings read/write never leak a token, inbox
actions, and the approve link (GET shows a button, POST acts, single-use)."""
import base64
import dataclasses
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent.tools import review_gate
from agent import config as agent_config
from agent import github_inbox as gi
from agent import github_settings as gs
from agent.auth import User

_ADMIN = User(id=1, email="admin@example.com", role="admin", allowed_repos=None,
              totp_enabled=True, must_change_password=False,
              auto_approve_commands=False, require_merge_review=True)


class FakeStore:
    def __init__(self):
        self.data = {}

    async def asearch(self, ns, limit=100):
        return [SimpleNamespace(key=k, value=v) for (n, k), v in self.data.items() if n == ns]

    async def aput(self, ns, key, value):
        self.data[(ns, key)] = dict(value)

    async def aget(self, ns, key):
        v = self.data.get((ns, key))
        return SimpleNamespace(key=key, value=v) if v is not None else None


@pytest.fixture
def wired(monkeypatch):
    store = FakeStore()
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    # Config is a frozen dataclass: swap the module's instance, not its fields.
    fake = dataclasses.replace(srv.config, auth_secret_key=key, github_token=None)
    monkeypatch.setattr(srv, "config", fake)
    # And on app.state, which is where the extracted seams read it from
    # (agent/routers/). Patching only the module global stopped reaching the
    # settings routes the moment they moved out of server.py.
    monkeypatch.setattr(srv.app.state, "config", fake)
    monkeypatch.setattr(agent_config, "PROJECTS", {"proj": {"live": "/nowhere", "sandbox": "/nowhere"}}, raising=False)
    monkeypatch.setattr(srv, "PROJECTS", agent_config.PROJECTS, raising=False)
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    monkeypatch.setattr(gs, "_cache", gs.normalize(None))
    created = []

    async def fake_create(repo, goal, budget, route):
        created.append((repo, goal, budget, route))
        return "task-1"

    monkeypatch.setattr(srv, "_github_create_task", fake_create)
    # The inbox routes live in agent/routers/github.py and reach the creator
    # on app.state (a router cannot import it from server.py).
    monkeypatch.setattr(srv.app.state, "github_create_task", fake_create)
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    return {"store": store, "created": created}


def test_settings_round_trip_never_returns_the_token(wired):
    client = TestClient(srv.app)
    body = client.get("/api/settings/github").json()
    assert body["settings"]["tokens"] == {} and "dependabot_prs" in body["sources"]

    res = client.post("/api/settings/github", json={
        "add_tokens": {"main": "github_pat_" + "z" * 40},
        "public_url": "https://agent.example.com/v2",
        "projects": {"proj": {"token": "main", "policies": {"dependabot_prs": "propose"}, "budget_usd": 4}},
    })
    assert res.status_code == 200, res.text
    s = res.json()["settings"]
    assert s["tokens"]["main"]["hint"] == "…zzzz" and "enc" not in s["tokens"]["main"]
    assert "github_pat_" not in res.text
    assert s["projects"]["proj"]["policies"]["dependabot_prs"] == "propose"

    # persisted: a fresh load from the store sees it, and the ciphertext is there, not the token
    stored = wired["store"].data[(("settings",), "github")]
    assert "github_pat_" not in str(stored) and stored["tokens"]["main"]["enc"]

    res = client.post("/api/settings/github", json={"projects": {"proj": {"policies": {"dependabot_prs": "sometimes"}}}})
    assert res.status_code == 400


def _seed_proposed(store, nonce="n1"):
    item = gi.Item(key="pr:7", kind="dependabot_prs", repo="proj", title="bump x", url="https://gh/pr/7",
                   fingerprint="f", number=7, state="proposed", mode="propose", approval_nonce=nonce)
    store.data[(("github_inbox", "proj"), "pr:7")] = item.to_dict()
    return item


def test_inbox_lists_and_dashboard_actions_work(wired):
    client = TestClient(srv.app)
    _seed_proposed(wired["store"])
    body = client.get("/api/github/inbox").json()
    assert [i["key"] for i in body["items"]] == ["pr:7"]

    res = client.post("/api/github/inbox/proj/pr:7/snooze", json={"days": 2})
    assert res.status_code == 200 and res.json()["item"]["state"] == "snoozed"

    res = client.post("/api/github/inbox/proj/pr:7/approve")
    assert res.status_code == 200, res.text
    assert res.json()["task_id"] == "task-1"
    assert wired["created"][0][0] == "proj" and "pull request #7" in wired["created"][0][1]

    # approving again does not start a second task
    res = client.post("/api/github/inbox/proj/pr:7/approve")
    assert res.status_code == 200 and res.json().get("already") is True and len(wired["created"]) == 1

    assert client.post("/api/github/inbox/proj/nope/dismiss").status_code == 404
    assert client.post("/api/github/inbox/other/pr:7/dismiss").status_code == 404


def test_approve_link_get_only_shows_a_button_and_post_is_single_use(wired):
    client = TestClient(srv.app)
    _seed_proposed(wired["store"], nonce="n1")
    tok = gi.sign_approval(srv.config, "proj", "pr:7", "n1", "approve")

    page = client.get(f"/api/github/approve?t={tok}")
    assert page.status_code == 200 and "Approve and start" in page.text and "bump x" in page.text
    assert wired["created"] == [], "a GET (link preview) must never act"

    done = client.post("/api/github/approve", data={"t": tok})
    assert done.status_code == 200 and "Task started" in done.text
    assert len(wired["created"]) == 1

    again = client.post("/api/github/approve", data={"t": tok})
    assert "already used" in again.text and len(wired["created"]) == 1
    page = client.get(f"/api/github/approve?t={tok}")
    assert "Already handled" in page.text

    bad = client.get("/api/github/approve?t=nonsense")
    assert "malformed" in bad.text
    expired = gi.sign_approval(srv.config, "proj", "pr:7", "n1", "approve", ttl_s=-5)
    assert "expired" in client.get(f"/api/github/approve?t={expired}").text


def test_dismiss_link_dismisses(wired):
    client = TestClient(srv.app)
    _seed_proposed(wired["store"], nonce="n2")
    tok = gi.sign_approval(srv.config, "proj", "pr:7", "n2", "dismiss")
    assert "Dismiss" in client.get(f"/api/github/approve?t={tok}").text
    assert "Dismissed" in client.post("/api/github/approve", data={"t": tok}).text
    assert wired["store"].data[(("github_inbox", "proj"), "pr:7")]["state"] == "dismissed"
    assert wired["created"] == []


# ---------------------------------------------------------------------------
# Auto is refused for a project that verifies nothing (2026-09-11)
# ---------------------------------------------------------------------------

def _checks(monkeypatch, value):
    async def _has_checks(repo):
        return value
    # Patched on the module itself rather than through agent.server: the
    # settings routes moved into agent/routers/settings.py when server.py
    # started being split, and server.py no longer imports review_gate at all.
    monkeypatch.setattr(review_gate, "project_has_checks", _has_checks)


def test_setting_auto_is_refused_when_the_project_has_no_checks(wired, monkeypatch):
    _checks(monkeypatch, False)
    client = TestClient(srv.app)
    res = client.post("/api/settings/github",
                      json={"projects": {"proj": {"policies": {"dependabot_prs": "auto"}}}})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "no checks" in detail and "Propose" in detail, detail
    # ...and nothing was saved.
    assert srv.github_settings.current()["projects"].get("proj", {}).get("policies", {}).get("dependabot_prs", "off") != "auto"


def test_setting_auto_is_refused_when_the_reviewer_cannot_be_reached(wired, monkeypatch):
    _checks(monkeypatch, None)
    res = TestClient(srv.app).post("/api/settings/github",
                                   json={"projects": {"proj": {"policies": {"security_alerts": "auto"}}}})
    assert res.status_code == 400
    assert "could not confirm" in res.json()["detail"]


def test_propose_is_never_refused(wired, monkeypatch):
    """The rule is about starting work unattended. Listing it is always fine,
    and refusing Propose would push an operator towards turning the inbox off."""
    _checks(monkeypatch, False)
    res = TestClient(srv.app).post("/api/settings/github",
                                   json={"projects": {"proj": {"policies": {"dependabot_prs": "propose"}}}})
    assert res.status_code == 200


def test_auto_is_allowed_for_a_project_with_checks(wired, monkeypatch):
    _checks(monkeypatch, True)
    res = TestClient(srv.app).post("/api/settings/github",
                                   json={"projects": {"proj": {"policies": {"dependabot_prs": "auto"}}}})
    assert res.status_code == 200
    assert res.json()["settings"]["projects"]["proj"]["policies"]["dependabot_prs"] == "auto"
