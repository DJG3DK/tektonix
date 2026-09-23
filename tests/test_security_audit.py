"""Regressions from the 2026-08-24 security audit of this repo.

Each of these was a live defect, not a hypothetical. They share a shape:
a guard that existed but did not actually cover the path in question.
"""

import os
import tempfile

import pytest

from agent.tools.agent_tools import make_agent_tools
from agent.tools.files import PathEscapeError, _resolve


# ---------------------------------------------------------------------------
# describe_image escaped the repo entirely
# ---------------------------------------------------------------------------
# read/write/edit all routed through _resolve(), which enforces containment.
# describe_image used os.path.join(repo_root, path) instead -- and os.path.join
# DISCARDS the root when the second argument is absolute. So
# describe_image("/home/3d-agent/.env") read that file off the host and sent
# its contents to the vision model. The mime check did not help: an
# extensionless file guesses to None, which the code defaulted to "image/png",
# so precisely the files worth stealing passed it. This tool runs host-side,
# not in the bash container, so nothing else stood behind it.

def _describe_image_tool(repo_root):
    tools, _ = make_agent_tools(repo_root)
    return next(t for t in tools if t.name == "describe_image")


@pytest.mark.parametrize("escape", [
    "/etc/hostname",
    "/etc/passwd",
    "../../../etc/hostname",
    "../../../../root/.ssh/id_rsa",
])
async def test_describe_image_refuses_to_leave_the_repo(escape):
    with tempfile.TemporaryDirectory() as repo:
        result = await _describe_image_tool(repo).ainvoke({"path": escape})
        assert result.startswith("ERROR")
        # And specifically not because the vision call failed -- it must be
        # refused before any file is read.
        assert "vision call failed" not in result


async def test_describe_image_refuses_unknown_file_types():
    """An extensionless secret used to be treated as image/png by default."""
    with tempfile.TemporaryDirectory() as repo:
        secret = os.path.join(repo, "id_rsa")
        with open(secret, "w") as fh:
            fh.write("-----BEGIN OPENSSH PRIVATE KEY-----")
        result = await _describe_image_tool(repo).ainvoke({"path": "id_rsa"})
        assert result.startswith("ERROR")
        assert "not a recognised image" in result


def test_resolve_still_blocks_the_same_escapes():
    """The shared guard describe_image now uses."""
    with tempfile.TemporaryDirectory() as repo:
        for escape in ("/etc/passwd", "../../etc/passwd"):
            with pytest.raises(PathEscapeError):
                _resolve(repo, escape)
        # ...while ordinary repo-relative paths still resolve.
        assert str(_resolve(repo, "src/App.tsx")).startswith(os.path.realpath(repo))


# ---------------------------------------------------------------------------
# Authorization failed OPEN when a task's repo could not be resolved
# ---------------------------------------------------------------------------
# `_resolve_task_repo` returns None for a missing/unreadable checkpoint. Three
# call sites read `if repo: check_repo_access(...)` -- so an unresolvable repo
# skipped the check entirely and any authenticated user could act on the task.

def test_task_endpoints_fail_closed_on_unresolvable_repo(monkeypatch):
    """A task whose repo cannot be resolved must be REFUSED, not allowed.

    This used to assert on inspect.getsource() substrings, which the project's
    own guidance rejects and which passes as soon as the right words appear.
    It now drives the endpoints: with repo resolution returning None, a
    non-admin scoped to nothing must be rejected rather than served.
    """
    from fastapi.testclient import TestClient

    import agent.server as srv
    from agent.auth import User

    scoped = User(id=2, email="dev@example.com", role="user", allowed_repos=["only-this"],
                  totp_enabled=True, must_change_password=False,
                  auto_approve_commands=False, require_merge_review=True)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: scoped)
    monkeypatch.setattr(srv.app.state, "store", object(), raising=False)

    async def unresolvable(task_id):
        return None

    from agent.routers import tasks as tasks_routes
    monkeypatch.setattr(tasks_routes, "_resolve_task_repo", lambda app, task_id: unresolvable(task_id))
    client = TestClient(srv.app)

    for method, path, payload in (
        ("post", "/api/tasks/unknown-task/message", {"text": "hi"}),
        ("post", "/api/tasks/unknown-task/stop", None),
    ):
        res = getattr(client, method)(path, **({"json": payload} if payload else {}))
        # Any 4xx refusal is correct; the property under test is that the
        # request does not SUCCEED. Pinning an exact code would make this
        # brittle against unrelated ordering changes (the message endpoint
        # answers 409 because it checks liveness first).
        assert 400 <= res.status_code < 500, (
            f"{path} returned {res.status_code} for a task whose repo could not be "
            "resolved — that is the fail-open this guards")
