"""The two WebSockets refuse before the handshake, through the real handler.

tests/test_route_inventory.py only checks that each handler's source mentions
get_user_from_ws_cookie. These connect: no cookie closes 4401; a signed-in user
held at a forced screen, or without access to the repo, closes 4403. The
cookie is read by the real helper -- only the session lookup it ends in (a
database query) is stood in for.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import agent.server as srv
from agent import auth
from agent.auth import User

ROUTES = ["/api/tasks/t-1/stream", "/api/planning/sessions/s-1/stream"]


def _user(**over):
    base = dict(id=7, email="u@example.com", role="user", allowed_repos=["other-repo"],
                totp_enabled=True, must_change_password=False,
                auto_approve_commands=False, require_merge_review=True)
    base.update(over)
    return User(**base)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)

    async def task_repo(task_id):
        return "proj"

    async def planning_meta(session_id):
        return "proj", {"session_id": session_id, "repo": "proj"}

    from agent.routers import tasks as tasks_routes
    monkeypatch.setattr(tasks_routes, "_resolve_task_repo", lambda app, task_id: task_repo(task_id))
    monkeypatch.setattr(srv, "_find_planning_meta", planning_meta)
    return TestClient(srv.app)


def _close_code(client, route, cookie=None):
    if cookie:
        client.cookies.set(auth.SESSION_COOKIE_NAME, cookie)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(route) as ws:
            ws.receive_text()
    return e.value.code


@pytest.mark.parametrize("route", ROUTES)
def test_no_cookie_is_closed_4401(client, route):
    assert _close_code(client, route) == 4401


@pytest.mark.parametrize("route", ROUTES)
def test_a_dead_session_is_closed_4401(client, monkeypatch, route):
    async def resolve(pool, token):
        return None
    monkeypatch.setattr(auth, "resolve_session", resolve)
    assert _close_code(client, route, cookie="expired-token") == 4401


@pytest.mark.parametrize("route", ROUTES)
def test_a_user_without_the_repo_is_closed_4403(client, monkeypatch, route):
    async def resolve(pool, token):
        return _user(allowed_repos=["other-repo"])
    monkeypatch.setattr(auth, "resolve_session", resolve)
    assert _close_code(client, route, cookie="live-token") == 4403


@pytest.mark.parametrize("route", ROUTES)
def test_an_admin_without_2fa_is_held_at_the_forced_screen(client, monkeypatch, route):
    async def resolve(pool, token):
        return _user(role="admin", allowed_repos=None, totp_enabled=False)
    monkeypatch.setattr(auth, "resolve_session", resolve)
    assert _close_code(client, route, cookie="live-token") == 4403
