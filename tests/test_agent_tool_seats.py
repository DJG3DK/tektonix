"""Which seat gets which tool.

Three things are easy to get wrong here and invisible when they are, because
a missing tool does not fail -- the model simply says it cannot do the thing
and works around it:

  * the investigator's prompt has described `preview_app` since the tool
    existed, while the tool was bound only to the coordinator. A model that
    followed its own instructions got "no such tool";
  * a build task is scoped to one repository on purpose, so the tools that
    reach ANOTHER project must appear only when the task is actually allowed
    one, and must never appear for its own repo;
  * the planner is cross-project, so its preview must take the repo as an
    argument rather than being bound to the session's.
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

import agent.deep_agent as da
import agent.planning_chat as pc
from agent.config import load_config


def _capture(monkeypatch, module):
    captured = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(module, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(module, "llm_for_role", lambda *a, **k: FakeListChatModel(responses=["x"]))
    return captured


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    projects = {
        "demo": {"sandbox": str(tmp_path / "demo")},
        "other": {"sandbox": str(tmp_path / "other")},
    }
    for p in projects.values():
        import os
        os.makedirs(p["sandbox"], exist_ok=True)
    monkeypatch.setattr(da, "PROJECTS", projects, raising=False)
    monkeypatch.setattr(pc, "PROJECTS", projects, raising=False)
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", projects)
    monkeypatch.setattr("agent.tools.reference_tools.PROJECTS", projects)
    return load_config(), "demo", InMemorySaver(), InMemoryStore()


def _names(tools) -> set:
    return {t.name for t in tools}


def _seat(captured, name) -> dict:
    return next(s for s in captured["subagents"] if s["name"] == name)


# --- seeing the app -------------------------------------------------------

async def test_the_coordinator_can_run_the_app_and_look_at_it(two_projects, monkeypatch):
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    assert {"preview_app", "browse_page"} <= _names(captured["tools"])


async def test_the_investigator_has_the_tool_its_prompt_describes(two_projects, monkeypatch):
    """It is the seat that gets sent to find out what a page currently does,
    and it already has `bash` in the same sandbox -- so it could always START
    a server; what it could not do was see one."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    inv = _seat(captured, "investigator")
    assert {"preview_app", "browse_page"} <= _names(inv["tools"])
    assert "preview_app" in inv["system_prompt"], "described but not given is the bug"


async def test_the_investigator_still_cannot_write(two_projects, monkeypatch):
    """preview_app is not a write tool, and adding it must not have brought
    one along."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    inv = _names(_seat(captured, "investigator")["tools"])
    assert not (inv & {"write", "edit", "write_file", "edit_file"})


# --- another project as a reference ---------------------------------------

async def test_no_reference_tools_when_the_task_may_read_nothing_else(two_projects, monkeypatch):
    """A task with no reference scope is not offered four tools that can only
    refuse, and its prompt does not mention them."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    assert not (_names(captured["tools"]) & {"read_project_file", "search_project"})
    assert "ANOTHER PROJECT AS A REFERENCE" not in captured["system_prompt"]


async def test_its_own_repo_alone_is_not_a_reference_scope(two_projects, monkeypatch):
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store, reference_repos=["demo"])
    assert not (_names(captured["tools"]) & {"read_project_file", "search_project"})


async def test_the_reference_tools_appear_when_another_project_is_allowed(two_projects, monkeypatch):
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store, reference_repos=["demo", "other"])
    assert {"read_project_file", "search_project", "list_project_dir", "find_files"} \
        <= _names(captured["tools"])


async def test_the_prompt_names_the_projects_and_says_they_are_read_only(two_projects, monkeypatch):
    """A model asked to "do it like the other project" will otherwise say it
    has no way to see that project."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store, reference_repos=["demo", "other"])
    prompt = captured["system_prompt"]
    assert "ANOTHER PROJECT AS A REFERENCE" in prompt
    assert "other" in prompt and "READ-ONLY" in prompt
    assert "Borrow the approach, not the file" in prompt


async def test_the_investigator_gets_them_too(two_projects, monkeypatch):
    """It is the seat the coordinator delegates reading to."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store, reference_repos=["demo", "other"])
    inv = _seat(captured, "investigator")
    assert "read_project_file" in _names(inv["tools"])
    assert "ANOTHER PROJECT AS A REFERENCE" in inv["system_prompt"]


async def test_the_test_writer_does_not_get_them(two_projects, monkeypatch):
    """It writes tests for this repo against this repo's suite."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store, reference_repos=["demo", "other"])
    assert "read_project_file" not in _names(_seat(captured, "test-writer")["tools"])


# --- the planner ----------------------------------------------------------

async def test_the_planner_has_preview_app(two_projects, monkeypatch):
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    assert "preview_app" in _names(captured["tools"])


async def test_the_planners_preview_names_the_repo(two_projects, monkeypatch):
    """Planning is cross-project, so its preview cannot be bound to one
    workspace the way a build task's is."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    tool = next(t for t in captured["tools"] if t.name == "preview_app")
    assert "repo" in tool.args, "the planner's preview must take the project as an argument"


# --- how the scope gets there ---------------------------------------------
#
# A task runs under the access its CREATOR had at the time, the same rule
# auto_approve_commands and require_merge_review already follow. The list is
# concrete rather than "None means everything", so a task checkpointed before
# this existed falls back to its own repo alone instead of silently to all of
# them.

def test_an_admin_may_reference_every_project(monkeypatch):
    import agent.server as srv
    from agent.auth import User

    monkeypatch.setattr(srv, "PROJECTS", {"a": {}, "b": {}}, raising=False)
    admin = User(id=1, email="a@e", role="admin", allowed_repos=None, totp_enabled=True,
                 must_change_password=False, auto_approve_commands=False,
                 require_merge_review=True)
    assert srv._readable_repos(admin) == ["a", "b"]


def test_a_restricted_user_may_reference_only_their_own(monkeypatch):
    import agent.server as srv
    from agent.auth import User

    monkeypatch.setattr(srv, "PROJECTS", {"a": {}, "b": {}}, raising=False)
    user = User(id=2, email="d@e", role="user", allowed_repos=["a", "gone"], totp_enabled=True,
                must_change_password=False, auto_approve_commands=False,
                require_merge_review=True)
    assert srv._readable_repos(user) == ["a"], "a project they list but that no longer exists"


def test_a_task_carries_the_scope_it_was_created_with():
    from agent.outer_state import initial_state

    state = initial_state(task_id="t", goal="g", repo="a", budget_usd=1.0,
                          reference_repos=["a", "b"])
    assert state["reference_repos"] == ["a", "b"]


def test_a_task_created_without_one_references_nothing():
    from agent.outer_state import initial_state

    state = initial_state(task_id="t", goal="g", repo="a", budget_usd=1.0)
    assert state["reference_repos"] == [], "the default is its own repo alone"


def test_the_work_node_passes_the_scope_through():
    """The field is useless if the one caller of build_deep_agent drops it,
    and nothing else in the system would notice."""
    import inspect

    from agent.nodes import work

    src = inspect.getsource(work)
    assert 'reference_repos=state.get("reference_repos")' in src
