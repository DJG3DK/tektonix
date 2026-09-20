"""A snapshot of every route and the guard protecting it.

agent/server.py is 3,700 lines with 60+ routes in one namespace, and the
intended fix is to extract it into per-domain routers. The danger in that
refactor is not that it breaks loudly -- it is that a route quietly loses its
authentication, or disappears, and the existing tests (which mostly exercise
the modules underneath rather than the HTTP surface) stay green.

This is the safety net for that work: it pins the full inventory. Moving a
route between modules leaves this untouched; dropping one, changing its path
or method, or removing its auth dependency fails it immediately.

When a route is added or deliberately changed, update EXPECTED below in the
same commit -- the diff is then the reviewable record of what changed about
the app's surface.
"""

import inspect

from fastapi.routing import APIRoute, APIWebSocketRoute

import agent.server as srv

# Routes that are deliberately reachable without a session, and why.
PUBLIC_ROUTES = {
    ("POST", "/api/auth/login"),            # the entry point itself
    ("POST", "/api/auth/2fa/verify"),       # second factor, mid-login
    ("POST", "/api/auth/logout"),           # must work with a dead session
    ("POST", "/api/auth/forgot-password"),  # by definition pre-auth
    ("POST", "/api/auth/reset-password"),   # ditto, guarded by the emailed code
    # The approve link from a Telegram/email alert: the operator is on a phone
    # with no session. Guarded by the HMAC-signed, expiring, single-use token in
    # the URL (agent/github_inbox.py); the GET only renders a button, the POST acts.
    # Liveness for a monitoring box or a second person with curl, neither of
    # which has a session. Reports whether each dependency answers, never a
    # secret's value (agent/health.py).
    ("GET", "/api/health"),
    ("GET", "/api/github/approve"),
    ("POST", "/api/github/approve"),
}


def _auth_dependency(endpoint) -> str | None:
    """How a route authenticates.

    Two mechanisms are in use and both must be recognised, or the check gives
    a false alarm on routes that ARE guarded:

      * an injected dependency -- require_full_auth for most routes,
        get_current_user for the ones reachable mid-login (2FA setup, change
        password) that must work before the forced-screen gates pass;
      * in-body authentication, which is how the WebSockets do it: FastAPI
        cannot inject a dependency before the handshake, so they read the
        session cookie themselves and close with 4401/4403.
    """
    for _name, param in inspect.signature(endpoint).parameters.items():
        dep = getattr(param.default, "dependency", None)
        if dep is not None:
            return dep.__name__
    src = inspect.getsource(endpoint)
    if "get_user_from_ws_cookie" in src:
        return "get_user_from_ws_cookie (in-body, pre-handshake)"
    return None


# Static-serving routes, excluded from the snapshot. They serve the built
# bundle rather than data, and -- the reason this matters -- they are
# registered ONLY when frontend/dist exists, so including them would make the
# snapshot depend on whether the frontend happens to be built. That is not a
# hypothetical: this test caught it on its own first merge, comparing a
# worktree without a build against a checkout with one, and it would have
# failed in CI too, where the frontend is a separate job.
_STATIC_PATHS = {"/", "/{full_path:path}"}


def _inventory():
    """The API surface: every data route with its path, method and guard."""
    rows = []
    for r in srv.app.routes:
        if getattr(r, "path", None) in _STATIC_PATHS:
            continue
        if isinstance(r, APIRoute):
            for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                rows.append((method, r.path, _auth_dependency(r.endpoint)))
        elif isinstance(r, APIWebSocketRoute):
            rows.append(("WS", r.path, _auth_dependency(r.endpoint)))
    return sorted(rows)


def test_every_route_is_authenticated_or_explicitly_public():
    """The property that matters most: no route reaches application logic
    without either a session or a deliberate entry in PUBLIC_ROUTES."""
    unguarded = []
    for method, path, dep in _inventory():
        if dep is not None:
            continue
        if (method, path) in PUBLIC_ROUTES:
            continue
        unguarded.append(f"{method} {path}")
    assert not unguarded, (
        "these routes declare no auth dependency and are not listed as public:\n  "
        + "\n  ".join(unguarded))


def test_websocket_streams_are_authenticated():
    """Both streams carry live task output; neither may be open."""
    ws = [(m, p, d) for m, p, d in _inventory() if m == "WS"]
    assert ws, "the websocket routes disappeared"
    for _m, path, dep in ws:
        assert dep is not None, f"websocket {path} lost its auth dependency"


def test_the_route_surface_has_not_silently_changed():
    """Pins path, method and guard for every route.

    Fails when a route is added, removed, renamed, changes method, or changes
    its auth dependency -- including as a side effect of moving code between
    modules, which is exactly what this exists to catch.
    """
    actual = _inventory()
    assert len(actual) == len(EXPECTED), (
        f"route count changed: {len(EXPECTED)} -> {len(actual)}.\n"
        "If this is intended, update EXPECTED in this file in the same commit."
    )
    assert actual == EXPECTED, (
        "the route surface changed. Differences:\n  "
        + "\n  ".join(
            f"{a} != {b}" for a, b in zip(actual, EXPECTED, strict=False) if a != b
        )
    )


# Generated with:
#   python -c "import tests.test_route_inventory as t; print(t._inventory())"
EXPECTED: list[tuple[str, str, str | None]] = [
    ('DELETE', '/_review/{path:path}', 'require_full_auth'),
    ('DELETE', '/api/auth/users/{user_id}', 'require_full_auth'),
    ('DELETE', '/api/planning/sessions/{session_id}', 'require_full_auth'),
    ('DELETE', '/api/projects/archives/{filename}', 'require_full_auth'),
    ('DELETE', '/api/projects/{name}', 'require_full_auth'),
    ('DELETE', '/api/projects/{name}/deploy-key', 'require_full_auth'),
    ('DELETE', '/api/tasks/{task_id}', 'require_full_auth'),
    ('GET', '/_review/{path:path}', 'require_full_auth'),
    ('GET', '/api/analytics', 'require_full_auth'),
    ('GET', '/api/analytics/models', 'require_full_auth'),
    ('GET', '/api/analytics/tool-reliability', 'require_full_auth'),
    ('GET', '/api/analytics/trace-summary', 'require_full_auth'),
    ('GET', '/api/audit', 'require_full_auth'),
    ('GET', '/api/auth/me', 'get_current_user'),
    ('GET', '/api/auth/me/telegram', 'require_full_auth'),
    ('GET', '/api/auth/users', 'require_full_auth'),
    ('GET', '/api/consolidation/status', 'require_full_auth'),
    ('GET', '/api/env-config', 'require_full_auth'),
    ('GET', '/api/github/approve', None),
    ('GET', '/api/github/inbox', 'require_full_auth'),
    ('GET', '/api/health', None),
    ('GET', '/api/model-config', 'require_full_auth'),
    ('GET', '/api/model-config/catalog', 'require_full_auth'),
    ('GET', '/api/model-config/endpoints', 'require_full_auth'),
    ('GET', '/api/planning/sessions', 'require_full_auth'),
    ('GET', '/api/planning/sessions/{session_id}', 'require_full_auth'),
    ('GET', '/api/projects', 'require_full_auth'),
    ('GET', '/api/projects/archives', 'require_full_auth'),
    ('GET', '/api/projects/{name}/deploy-key', 'require_full_auth'),
    ('GET', '/api/push/key', 'require_full_auth'),
    ('GET', '/api/repos', 'require_full_auth'),
    ('GET', '/api/router-balance', 'require_full_auth'),
    ('GET', '/api/settings/github', 'require_full_auth'),
    ('GET', '/api/settings/runtime', 'require_full_auth'),
    ('GET', '/api/stats', 'require_full_auth'),
    ('GET', '/api/tasks', 'require_full_auth'),
    ('GET', '/api/tasks/{task_id}', 'require_full_auth'),
    ('GET', '/api/tasks/{task_id}/diff', 'require_full_auth'),
    ('PATCH', '/_review/{path:path}', 'require_full_auth'),
    ('PATCH', '/api/auth/users/{user_id}', 'require_full_auth'),
    ('POST', '/_review/{path:path}', 'require_full_auth'),
    ('POST', '/api/auth/2fa/confirm', 'get_current_user'),
    ('POST', '/api/auth/2fa/disable', 'require_full_auth'),
    ('POST', '/api/auth/2fa/setup', 'get_current_user'),
    ('POST', '/api/auth/2fa/verify', None),
    ('POST', '/api/auth/change-password', 'get_current_user'),
    ('POST', '/api/auth/forgot-password', None),
    ('POST', '/api/auth/login', None),
    ('POST', '/api/auth/logout', None),
    ('POST', '/api/auth/me/auto-approve', 'require_full_auth'),
    ('POST', '/api/auth/me/merge-review', 'require_full_auth'),
    ('POST', '/api/auth/me/telegram', 'require_full_auth'),
    ('POST', '/api/auth/me/telegram/test', 'require_full_auth'),
    ('POST', '/api/auth/me/theme', 'require_full_auth'),
    ('POST', '/api/auth/reset-password', None),
    ('POST', '/api/auth/users', 'require_full_auth'),
    ('POST', '/api/env-config', 'require_full_auth'),
    ('POST', '/api/env-config/restart', 'require_full_auth'),
    ('POST', '/api/github/approve', None),
    ('POST', '/api/github/inbox/{repo}/{key}/{action}', 'require_full_auth'),
    ('POST', '/api/github/poll', 'require_full_auth'),
    ('POST', '/api/model-config', 'require_full_auth'),
    ('POST', '/api/model-config/probe-forced-tool-call', 'require_full_auth'),
    ('POST', '/api/model-config/providers', 'require_full_auth'),
    ('POST', '/api/model-config/restart-router', 'require_full_auth'),
    ('POST', '/api/planning/sessions', 'require_full_auth'),
    ('POST', '/api/planning/sessions/{session_id}/archive', 'require_full_auth'),
    ('POST', '/api/planning/sessions/{session_id}/message', 'require_full_auth'),
    ('POST', '/api/planning/sessions/{session_id}/new-project', 'require_full_auth'),
    ('POST', '/api/planning/sessions/{session_id}/stop', 'require_full_auth'),
    ('POST', '/api/projects/create', 'require_full_auth'),
    ('POST', '/api/projects/detect', 'require_full_auth'),
    ('POST', '/api/projects/provision', 'require_full_auth'),
    ('POST', '/api/projects/{name}/deploy-key', 'require_full_auth'),
    ('POST', '/api/projects/{name}/deploy-key/generate', 'require_full_auth'),
    ('POST', '/api/projects/{name}/deploy-key/test', 'require_full_auth'),
    ('POST', '/api/push/subscribe', 'require_full_auth'),
    ('POST', '/api/push/test', 'require_full_auth'),
    ('POST', '/api/push/unsubscribe', 'require_full_auth'),
    ('POST', '/api/settings/github', 'require_full_auth'),
    ('POST', '/api/settings/github/test', 'require_full_auth'),
    ('POST', '/api/settings/runtime', 'require_full_auth'),
    ('POST', '/api/tasks', 'require_full_auth'),
    ('POST', '/api/tasks/{task_id}/approve', 'require_full_auth'),
    ('POST', '/api/tasks/{task_id}/merge-decision', 'require_full_auth'),
    ('POST', '/api/tasks/{task_id}/message', 'require_full_auth'),
    ('POST', '/api/tasks/{task_id}/resume', 'require_full_auth'),
    ('POST', '/api/tasks/{task_id}/stop', 'require_full_auth'),
    ('POST', '/api/uploads', 'require_full_auth'),
    ('PUT', '/_review/{path:path}', 'require_full_auth'),
    ('WS', '/api/planning/sessions/{session_id}/stream', 'get_user_from_ws_cookie (in-body, pre-handshake)'),
    ('WS', '/api/tasks/{task_id}/stream', 'get_user_from_ws_cookie (in-body, pre-handshake)')]
