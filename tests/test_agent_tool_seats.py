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
    import agent.config as agent_config_mod
    import agent.server as srv
    from agent.auth import User

    monkeypatch.setattr(agent_config_mod, "PROJECTS", {"a": {}, "b": {}}, raising=False)
    admin = User(id=1, email="a@e", role="admin", allowed_repos=None, totp_enabled=True,
                 must_change_password=False, auto_approve_commands=False,
                 require_merge_review=True)
    assert srv._readable_repos(admin) == ["a", "b"]


def test_a_restricted_user_may_reference_only_their_own(monkeypatch):
    import agent.config as agent_config_mod
    import agent.server as srv
    from agent.auth import User

    monkeypatch.setattr(agent_config_mod, "PROJECTS", {"a": {}, "b": {}}, raising=False)
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


# --- logo tools -----------------------------------------------------------
#
# The build coordinator designs and exports; the planner designs and does not
# export, because the planning chat mutates nothing by contract and a brand
# kit is two dozen binary files with nowhere to go in a conversation.

import agent.tools.logo_tools as _lt  # noqa: E402

_needs_logoloom = pytest.mark.skipif(not _lt.installed(), reason="LogoLoom not installed")


@_needs_logoloom
async def test_the_coordinator_can_design_and_export(two_projects, monkeypatch):
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    names = _names(captured["tools"])
    assert {"logo_render", "logo_text_to_path", "logo_optimize_svg",
            "logo_export_brand_kit"} <= names


@_needs_logoloom
async def test_the_coordinator_is_told_the_order_to_use_them_in(two_projects, monkeypatch):
    """Exporting before rendering makes two dozen copies of a bad logo."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    prompt = captured["system_prompt"]
    assert "MAKING A LOGO OR A BRAND KIT" in prompt
    assert prompt.index("logo_render") < prompt.index("logo_export_brand_kit")
    assert "nothing here designs one for you" in prompt


@_needs_logoloom
async def test_the_planner_can_design_but_not_export(two_projects, monkeypatch):
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    names = _names(captured["tools"])
    assert "logo_render" in names, "designing blind is guessing"
    assert "logo_export_brand_kit" not in names, "the planning chat writes to no repo"


@_needs_logoloom
async def test_the_test_writer_gets_no_logo_tools(two_projects, monkeypatch):
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    assert not (_names(_seat(captured, "test-writer")["tools"]) & {"logo_render",
                                                                  "logo_export_brand_kit"})


async def test_a_box_without_logoloom_gets_no_logo_tools_and_no_prompt(two_projects, monkeypatch):
    """install.sh makes the npm install optional, so this is the normal state
    of a fresh box -- not an error case."""
    monkeypatch.setattr(_lt, "BRIDGE", _lt.BRIDGE.parent / "definitely-not-here.mjs")
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    assert not (_names(captured["tools"]) & {"logo_render", "logo_export_brand_kit"})
    assert "MAKING A LOGO OR A BRAND KIT" not in captured["system_prompt"]


# --- what past tasks ran into ---------------------------------------------
#
# One table rather than a test per seat, because the next subsystem that adds
# a tool adds rows here instead of another block of near-identical tests --
# and because the interesting assertion is the shape of the whole column, not
# any one cell. The two that matter: the test-writer has neither (it writes
# tests for this repo against this repo's suite, and what some other task
# escalated over in June changes nothing about that), and the planner has
# both (it is the seat where "we tried that in June and it escalated" still
# changes something).

import agent.history_index as _hi  # noqa: E402
from agent.tools import history_tools as _ht  # noqa: E402


class _StubIndex:
    """Enough of an index for the tools to be built over. No seat test
    searches anything; what is under test is which seat is handed what."""

    async def search(self, query, **kw):
        return []

    async def fetch(self, *a):
        return None


@pytest.fixture
def with_history_index(monkeypatch):
    """An installation that HAS a history index, which CI otherwise is not.

    Installed rather than stubbed past, so the leg registry -- the thing that
    decides whether the tools exist at all -- is the same one the server uses.
    """
    _hi.install(_StubIndex())
    yield
    _hi.install(None)


_HISTORY_TOOLS = {"search_history", "read_history"}


@pytest.mark.parametrize("seat,expected", [
    ("coordinator", True),
    ("investigator", True),
    ("general-purpose", True),
    ("test-writer", False),
])
async def test_which_seats_can_search_history(two_projects, monkeypatch,
                                              with_history_index, seat, expected):
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    names = _names(captured["tools"] if seat == "coordinator" else _seat(captured, seat)["tools"])
    assert (_HISTORY_TOOLS <= names) is expected


async def test_the_planner_can_search_history(two_projects, monkeypatch, with_history_index):
    """The seat most likely to ask "have we tried this" -- and the one where
    the answer still changes the outcome rather than only the damage."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    assert _HISTORY_TOOLS <= _names(captured["tools"])


async def test_the_seats_that_have_it_are_told_when_to_use_it(two_projects, monkeypatch,
                                                              with_history_index):
    """Described but not given is one bug; given but not described is the
    other -- a model that is not told a capability exists says it cannot do
    the thing and works around it."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    assert "WHAT PAST TASKS RAN INTO" in captured["system_prompt"]
    assert "WHAT PAST TASKS RAN INTO" in _seat(captured, "investigator")["system_prompt"]
    # The general-purpose seat is handed both tools via *project_tools and
    # was the one seat never told they exist -- which is the half of the bug
    # this test's own docstring names.
    assert "WHAT PAST TASKS RAN INTO" in _seat(captured, "general-purpose")["system_prompt"]

    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    assert "WHAT PAST TASKS RAN INTO" in captured["system_prompt"]


async def test_searching_history_is_read_only(two_projects, monkeypatch, with_history_index):
    """It reads a table of its own and writes nothing, which is why the
    investigator can have it."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    inv = _names(_seat(captured, "investigator")["tools"])
    assert _HISTORY_TOOLS <= inv
    assert not (inv & {"write", "edit", "write_file", "edit_file"})


async def test_a_task_scoped_to_one_project_can_still_search_its_own_history(
        two_projects, monkeypatch, with_history_index):
    """The inversion from the reference tools, which appear only when there
    is ANOTHER project to read: history search is for your own project
    first, so it is there with no reference scope at all."""
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    assert _HISTORY_TOOLS <= _names(captured["tools"])
    assert not (_names(captured["tools"]) & {"read_project_file"})


async def test_a_box_with_no_history_index_gets_no_tools_and_no_prompt(two_projects, monkeypatch):
    """The normal state of a fresh install, and of every CI run -- not an
    error case. No seat carries a tool that could only report that search is
    unavailable."""
    _hi.install(None)
    cfg, repo, cp, store = two_projects
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    assert not (_names(captured["tools"]) & _HISTORY_TOOLS)
    assert "WHAT PAST TASKS RAN INTO" not in captured["system_prompt"]

    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    assert not (_names(captured["tools"]) & _HISTORY_TOOLS)
