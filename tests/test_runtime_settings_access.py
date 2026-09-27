"""Runtime settings are deployment-wide: an admin reads and writes them. The
one value every account needs -- what a new-task form prefills -- has its own
route, so the admin check does not take that away from non-admins."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User


def _user(role):
    return User(id=1, email="u@example.com", role=role, allowed_repos=None,
                totp_enabled=True, must_change_password=False)


@pytest.fixture
def as_role():
    client = TestClient(srv.app)

    def _as(role):
        srv.app.dependency_overrides[srv.require_full_auth] = lambda: _user(role)
        return client

    yield _as
    srv.app.dependency_overrides.clear()


def test_a_non_admin_cannot_read_the_runtime_knobs(as_role):
    assert as_role("user").get("/api/settings/runtime").status_code == 403


def test_an_admin_reads_the_runtime_knobs(as_role):
    res = as_role("admin").get("/api/settings/runtime")
    assert res.status_code == 200
    assert "default_task_budget_usd" in res.json()["values"]


def test_every_account_reads_the_task_defaults(as_role):
    for role in ("user", "admin"):
        res = as_role(role).get("/api/settings/task-defaults")
        assert res.status_code == 200
        assert list(res.json()) == ["default_task_budget_usd"], "nothing but the default"
        assert res.json()["default_task_budget_usd"] > 0
