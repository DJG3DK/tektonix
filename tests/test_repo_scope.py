"""Every repo-scoped route, and the check that keeps it to the caller's repos.

tests/test_route_inventory.py pins each route's AUTHENTICATION -- the
injected require_full_auth / get_current_user. It cannot see authorization:
check_repo_access(user, repo) is called inside the handler body, because the
repo arrives in too many shapes (a path segment, a body field, a task or
session looked up in the store) to be one clean Depends -- agent/auth.py
explains why. So a refactor could drop the call and every test stayed green.
The 2026-09-23 review found no live route missing it, and said the next seam
extraction is exactly when one would.

This is the net for that. A route is repo-scoped when its path or signature
carries a repo, task or planning session, when its request body has a `repo`
field, or when its handler resolves one from the store. Each such route is
pinned with the check that guards it, read from the handler's own source:

  * check_repo_access -- the in-body call;
  * can_access        -- user.can_access(repo) directly, which is what the
                         WebSockets do (they close 4403 instead of raising);
  * require_admin     -- admin-only routes, which see every repo by design.

A repo-scoped route with none of those fails, unless it is listed in
UNGUARDED with the reason it needs none. Moving a route between modules
leaves this untouched; dropping its check fails it.
"""
import inspect
import re
import typing

from fastapi.routing import APIRoute, APIWebSocketRoute

import agent.server as srv
from tests.test_route_inventory import _STATIC_PATHS, _walk

_SCOPED_PATH = re.compile(r"\{(task_id|session_id|repo|name)\}")
_SCOPED_PARAMS = {"repo", "task_id", "session_id"}
# Handlers that find their repo in the store rather than the request.
_RESOLVES_REPO = re.compile(r"_resolve_task_repo\(|_find_planning_meta\(|\(\"tasks\", ")

# Repo-scoped routes that deliberately carry no per-repo check, and why.
UNGUARDED: dict[tuple[str, str], str] = {}


def _body_has_repo(endpoint) -> bool:
    # Resolved type hints, not raw annotations: a router module written with
    # `from __future__ import annotations` stores them as STRINGS, and a
    # string has no model_fields -- which is how POST /api/tasks silently
    # stopped counting as repo-scoped when the tasks seam moved out.
    try:
        hints = typing.get_type_hints(endpoint)
    except Exception:  # noqa: BLE001 -- fall back to what the signature says
        hints = {n: p.annotation for n, p in inspect.signature(endpoint).parameters.items()}
    for ann in hints.values():
        fields = getattr(ann, "model_fields", None)
        if fields and "repo" in fields:
            return True
    return False


def _guard(src: str) -> str | None:
    if "check_repo_access(" in src:
        return "check_repo_access"
    if ".can_access(" in src:
        return "can_access"
    if "require_admin(" in src:
        return "require_admin"
    return None


def _scoped_routes():
    rows = []
    for r in _walk(srv.app.routes):
        if r.path in _STATIC_PATHS:
            continue
        ep = r.endpoint
        src = inspect.getsource(ep)
        scoped = (bool(_SCOPED_PATH.search(r.path))
                  or bool(set(inspect.signature(ep).parameters) & _SCOPED_PARAMS)
                  or _body_has_repo(ep)
                  or bool(_RESOLVES_REPO.search(src)))
        if not scoped:
            continue
        methods = ["WS"] if isinstance(r, APIWebSocketRoute) else sorted(r.methods - {"HEAD", "OPTIONS"})
        assert isinstance(r, (APIRoute, APIWebSocketRoute))
        for m in methods:
            rows.append((m, r.path, _guard(src)))
    return sorted(rows)


EXPECTED = [
    ('DELETE', '/api/planning/sessions/{session_id}', 'check_repo_access'),
    ('DELETE', '/api/projects/{name}', 'require_admin'),
    ('DELETE', '/api/projects/{name}/deploy-key', 'require_admin'),
    ('DELETE', '/api/tasks/{task_id}', 'check_repo_access'),
    ('GET', '/api/analytics', 'require_admin'),
    ('GET', '/api/artifacts/{repo}/{artifact_id}', 'check_repo_access'),   # images the agent showed
    ('GET', '/api/evals/runs/{name}', 'require_admin'),   # a run name, not a project; admin-only
    ('GET', '/api/github/inbox', 'check_repo_access'),
    ('GET', '/api/planning/sessions', 'check_repo_access'),
    ('GET', '/api/planning/sessions/{session_id}', 'check_repo_access'),
    ('GET', '/api/projects/{name}/checkout', 'require_admin'),
    ('GET', '/api/projects/{name}/deploy-key', 'require_admin'),
    ('GET', '/api/swebench/runs/{name}', 'require_admin'),   # a benchmark run, not a project; admin-only
    ('GET', '/api/swebench/runs/{name}/log', 'require_admin'),
    ('GET', '/api/swebench/runs/{name}/tasks/{instance_id}', 'require_admin'),
    ('GET', '/api/tasks', 'check_repo_access'),
    ('GET', '/api/tasks/{task_id}', 'check_repo_access'),
    ('GET', '/api/tasks/{task_id}/diff', 'check_repo_access'),
    ('GET', '/api/tasks/{task_id}/file', 'check_repo_access'),
    ('POST', '/api/github/inbox/{repo}/{key}/{action}', 'check_repo_access'),
    ('POST', '/api/planning/sessions', 'check_repo_access'),
    ('POST', '/api/planning/sessions/{session_id}/archive', 'check_repo_access'),
    ('POST', '/api/planning/sessions/{session_id}/message', 'check_repo_access'),
    ('POST', '/api/planning/sessions/{session_id}/new-project', 'check_repo_access'),
    ('POST', '/api/planning/sessions/{session_id}/stop', 'check_repo_access'),
    ('POST', '/api/projects/{name}/deploy-key', 'require_admin'),
    ('POST', '/api/projects/{name}/deploy-key/generate', 'require_admin'),
    ('POST', '/api/projects/{name}/deploy-key/test', 'require_admin'),
    ('POST', '/api/swebench/runs/{name}/stop', 'require_admin'),
    ('POST', '/api/tasks', 'check_repo_access'),
    ('POST', '/api/tasks/{task_id}/approve', 'check_repo_access'),
    ('POST', '/api/tasks/{task_id}/edits', 'check_repo_access'),
    ('POST', '/api/tasks/{task_id}/merge-decision', 'check_repo_access'),
    ('POST', '/api/tasks/{task_id}/message', 'check_repo_access'),
    ('POST', '/api/tasks/{task_id}/resume', 'check_repo_access'),
    ('POST', '/api/tasks/{task_id}/stop', 'check_repo_access'),
    ('POST', '/api/uploads', 'check_repo_access'),
    ('WS', '/api/planning/sessions/{session_id}/stream', 'can_access'),
    ('WS', '/api/tasks/{task_id}/stream', 'can_access'),
]


def test_no_repo_scoped_route_is_unguarded():
    missing = [(m, p) for m, p, g in _scoped_routes() if g is None and (m, p) not in UNGUARDED]
    assert not missing, (
        f"repo-scoped routes with no check_repo_access / can_access / require_admin: {missing}. "
        "Add the check, or list the route in UNGUARDED with the reason it needs none."
    )


def test_the_repo_scoped_surface_has_not_silently_changed():
    actual = _scoped_routes()
    assert actual == EXPECTED, (
        "the repo-scoped routes or their guards changed. If intended, update EXPECTED in the "
        "same commit so the diff records it.\n"
        f"added: {sorted(set(actual) - set(EXPECTED))}\nremoved: {sorted(set(EXPECTED) - set(actual))}"
    )


def test_the_unguarded_list_holds_only_real_routes():
    live = {(m, p) for m, p, _ in _scoped_routes()}
    assert set(UNGUARDED) <= live
