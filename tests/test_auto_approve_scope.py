"""Auto-approve is per project, not per account.

Global auto-approve was written for one operator who understood the sandbox.
The moment a second account exists it stops being safe by construction: an
admin who turned the switch on for a scratch project, or a new user handed
the same default, would run unattended against production too.

So the switch has two halves -- the operator's intent (a boolean) and where
they intended it (a list) -- and both must say yes. These tests pin the rule,
the endpoints that set it, and the backfill that keeps an existing
deployment's behaviour from changing under it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import auth
from agent.auth import User


def _user(**kw):
    base = dict(id=1, email="operator@example.com", role="admin", allowed_repos=None,
                totp_enabled=True, must_change_password=False,
                auto_approve_commands=True, require_merge_review=True,
                auto_approve_repos=["sandbox"])
    return User(**{**base, **kw})


# ---------------------------------------------------------------------------
# the rule itself
# ---------------------------------------------------------------------------

def test_both_halves_must_agree():
    u = _user()
    assert u.auto_approves("sandbox") is True
    assert u.auto_approves("production") is False, "a project the operator never named"


def test_the_switch_off_means_off_everywhere():
    u = _user(auto_approve_commands=False, auto_approve_repos=["sandbox", "production"])
    assert u.auto_approves("sandbox") is False


def test_an_unscoped_account_is_treated_as_no_projects():
    """NULL means 'never scoped'. Reading it as 'everywhere' would make the
    absence of a decision the widest possible setting."""
    u = _user(auto_approve_repos=None)
    assert u.auto_approves("sandbox") is False


def test_scope_does_not_grant_access_on_its_own():
    """auto_approve_repos is about prompting, never about reach: a project
    outside allowed_repos is still refused by can_access."""
    u = _user(role="user", allowed_repos=["sandbox"], auto_approve_repos=["sandbox", "production"])
    assert u.auto_approves("production") is True   # the switch says yes...
    assert u.can_access("production") is False     # ...and access still says no


# ---------------------------------------------------------------------------
# the endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(srv, "PROJECTS", {"sandbox": {}, "production": {}}, raising=False)
    monkeypatch.setattr(srv.app.state, "store", None, raising=False)
    updates: list[tuple] = []

    async def fake_update(pool, user_id, enabled, repos=None):
        updates.append((user_id, enabled, repos))

    monkeypatch.setattr(auth, "update_auto_approve", fake_update)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    c = TestClient(srv.app)
    c.updates = updates
    return c


def _as(client, user):
    srv.app.dependency_overrides[srv.require_full_auth] = lambda: user
    return client


def test_turning_it_on_must_name_the_projects(client):
    _as(client, _user(auto_approve_commands=False, auto_approve_repos=None))
    res = client.post("/api/auth/me/auto-approve", json={"auto_approve_commands": True})
    assert res.status_code == 400
    assert "projects it covers" in res.json()["detail"]
    assert client.updates == [], "nothing was changed"
    srv.app.dependency_overrides.clear()


def test_turning_it_on_with_an_empty_list_is_refused(client):
    _as(client, _user(auto_approve_commands=False, auto_approve_repos=None))
    res = client.post("/api/auth/me/auto-approve",
                      json={"auto_approve_commands": True, "repos": []})
    assert res.status_code == 400
    srv.app.dependency_overrides.clear()


def test_turning_it_on_for_named_projects_stores_exactly_those(client):
    _as(client, _user(auto_approve_commands=False, auto_approve_repos=None))
    res = client.post("/api/auth/me/auto-approve",
                      json={"auto_approve_commands": True, "repos": ["sandbox"]})
    assert res.status_code == 200
    assert res.json()["auto_approve_repos"] == ["sandbox"]
    assert client.updates == [(1, True, ["sandbox"])]
    srv.app.dependency_overrides.clear()


def test_an_unknown_project_is_refused(client):
    _as(client, _user(auto_approve_commands=False, auto_approve_repos=None))
    res = client.post("/api/auth/me/auto-approve",
                      json={"auto_approve_commands": True, "repos": ["not-a-project"]})
    assert res.status_code == 400
    srv.app.dependency_overrides.clear()


def test_a_user_cannot_scope_it_to_a_project_they_cannot_reach(client):
    _as(client, _user(role="user", allowed_repos=["sandbox"],
                      auto_approve_commands=False, auto_approve_repos=None))
    res = client.post("/api/auth/me/auto-approve",
                      json={"auto_approve_commands": True, "repos": ["production"]})
    assert res.status_code == 403
    srv.app.dependency_overrides.clear()


def test_turning_it_off_keeps_the_scope_for_next_time(client):
    """Turning the switch off should not make the operator re-pick every
    project when they turn it back on."""
    _as(client, _user(auto_approve_commands=True, auto_approve_repos=["sandbox"]))
    res = client.post("/api/auth/me/auto-approve", json={"auto_approve_commands": False})
    assert res.status_code == 200
    assert client.updates == [(1, False, None)], "scope untouched"
    srv.app.dependency_overrides.clear()


def test_the_user_payload_exposes_the_scope(client):
    # /api/auth/me answers before 2FA is complete, so it hangs off
    # get_current_user rather than require_full_auth.
    srv.app.dependency_overrides[auth.get_current_user] = lambda: _user()
    res = client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.json()["auto_approve_repos"] == ["sandbox"]
    srv.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# the backfill
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _Conn:
    def __init__(self, rowcount=1):
        self.rowcount, self.sql = rowcount, []

    async def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return _Cur(self.rowcount)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


def test_the_backfill_only_touches_accounts_that_were_never_scoped():
    conn = _Conn()
    n = asyncio.run(auth.backfill_auto_approve_repos(_Pool(conn), ["b", "a"]))
    sql, params = conn.sql[0]
    assert "auto_approve_repos IS NULL" in sql, "it must not overwrite a scope someone chose"
    assert "auto_approve_commands AND" in sql, "an account with the switch off needs no scope"
    assert params[0] == ["a", "b"], "sorted, so the stored value is stable"
    assert n == 1


def test_the_backfill_does_nothing_without_projects():
    conn = _Conn()
    assert asyncio.run(auth.backfill_auto_approve_repos(_Pool(conn), [])) == 0
    assert conn.sql == []
