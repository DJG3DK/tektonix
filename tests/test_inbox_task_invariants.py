"""Invariants for a task GitHub asked for, rather than a person.

ONE property is an invariant here, and these tests exist because a README
sentence is not: merge review is always required on an inbox task. Nothing it
does reaches the default branch without the operator approving the merge, no
matter what any account's preferences say. Auto inbox + merge-review-off is
the combination that would turn this from an assistant into an unattended
merge bot.

Auto-approve of gated file/shell actions is NOT an invariant, as of
2026-09-13. It was hard-coded False on the reasoning that nobody typed these
goals, and that sounded right and worked badly: Dependabot alert #2 -- a
CRITICAL Next.js RCE the inbox started itself -- parked at awaiting_approval
on `"eslint-config-next": "16.2.12" -> "16.3.5"`, because editing package.json
trips the sensitive-path gate. A dependency bump touches the manifest, the
lockfile and sometimes a workflow, so it asked once per file, while the
operator had Auto on for that very project. It follows the operator's
per-project switch now; the gate that guards the repo is untouched either way.
"""
import asyncio
import inspect

import pytest

import agent.auth as auth
import agent.server as srv


def _capture_start_task(monkeypatch):
    """Replace _start_task and return the kwargs it was called with."""
    seen: dict = {}

    async def fake_start_task(goal, repo, budget_usd, route, **kwargs):
        seen.update({"goal": goal, "repo": repo, "budget_usd": budget_usd, "route": route, **kwargs})
        return {"task_id": "task-1"}

    monkeypatch.setattr(srv, "_start_task", fake_start_task)
    return seen


def _accounts(monkeypatch, rows):
    """Stand in for the accounts table, at the layer repo_auto_approves reads."""
    async def fake_list_users(_pool):
        return rows

    monkeypatch.setattr(auth, "list_users", fake_list_users)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)


# ---------------------------------------------------------------------------
# the invariant
# ---------------------------------------------------------------------------

def test_an_inbox_task_always_requires_merge_review(monkeypatch):
    seen = _capture_start_task(monkeypatch)
    _accounts(monkeypatch, [{"role": "admin", "auto_approve_commands": True,
                             "auto_approve_repos": ["proj"], "require_merge_review": False}])

    task_id = asyncio.run(srv._github_create_task("proj", "fix the alert", 3.0, "auto"))

    assert task_id == "task-1"
    assert seen["require_merge_review"] is True, "an inbox task must always keep the operator's merge approval"
    assert seen["origin"] == "github"


def test_merge_review_is_not_read_from_any_preference():
    """A regression here would look like a one-word change, so pin the shape:
    the literal must stay, and no preference may reach it."""
    source = inspect.getsource(srv._github_create_task)
    assert "require_merge_review=True" in source
    assert "require_merge_review=auto" not in source
    assert "user.require_merge_review" not in source


# ---------------------------------------------------------------------------
# auto-approve now follows the operator's own per-project switch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rows,expected,why", [
    ([{"role": "admin", "auto_approve_commands": True, "auto_approve_repos": ["proj"]}],
     True, "auto on and scoped to this project"),
    ([{"role": "admin", "auto_approve_commands": True, "auto_approve_repos": ["other"]}],
     False, "auto on but scoped elsewhere -- the scope is the point"),
    ([{"role": "admin", "auto_approve_commands": False, "auto_approve_repos": ["proj"]}],
     False, "switch off"),
    ([{"role": "admin", "auto_approve_commands": True, "auto_approve_repos": None}],
     False, "never scoped is treated as no projects, same as User.auto_approves"),
    ([{"role": "user", "auto_approve_commands": True, "auto_approve_repos": ["proj"]}],
     False, "a non-admin preference must not widen what an unattended task may do"),
    ([], False, "no accounts at all"),
])
def test_auto_approve_follows_the_operators_scope(monkeypatch, rows, expected, why):
    seen = _capture_start_task(monkeypatch)
    _accounts(monkeypatch, rows)

    asyncio.run(srv._github_create_task("proj", "fix the alert", 3.0, "auto"))

    assert seen["auto_approve_commands"] is expected, why
    assert seen["require_merge_review"] is True, "whatever else changes, this does not"


def test_it_fails_closed_when_the_accounts_cannot_be_read(monkeypatch):
    """Guessing True here would mean an unattended task editing files nobody
    agreed to, so a database problem has to mean prompting."""
    seen = _capture_start_task(monkeypatch)

    async def boom(_pool):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(auth, "list_users", boom)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)

    asyncio.run(srv._github_create_task("proj", "fix the alert", 3.0, "auto"))
    assert seen["auto_approve_commands"] is False


def test_one_admin_with_the_project_scoped_is_enough(monkeypatch):
    """Several accounts, only one of them an admin who scoped this project."""
    seen = _capture_start_task(monkeypatch)
    _accounts(monkeypatch, [
        {"role": "user", "auto_approve_commands": False, "auto_approve_repos": None},
        {"role": "admin", "auto_approve_commands": True, "auto_approve_repos": ["a", "proj", "b"]},
    ])

    asyncio.run(srv._github_create_task("proj", "fix the alert", 3.0, "auto"))
    assert seen["auto_approve_commands"] is True


# ---------------------------------------------------------------------------
# both inbox paths still go through the one creator
# ---------------------------------------------------------------------------

def test_every_inbox_path_goes_through_the_same_creator():
    """Poller and dashboard-approve both create tasks; neither may build its
    own call to _start_task and skip the merge-review invariant."""
    from agent import github_inbox

    assert "create_task(" in inspect.getsource(github_inbox.create_task_for_item)
    from agent.routers import github as github_routes

    act = inspect.getsource(github_routes._github_act)
    # The route module reaches server.py's creator on app.state; the one it
    # puts there is _github_create_task itself (asserted below).
    assert "create_task_for_item" in act and "app.state.github_create_task" in act
    assert srv.app.state.github_create_task is srv._github_create_task
    poll = inspect.getsource(srv._github_poll_once)
    assert "_github_create_task" in poll
