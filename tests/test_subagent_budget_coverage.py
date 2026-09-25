"""Every subagent must carry the budget guard and the call limits.

BudgetGuardMiddleware is never inherited: deepagents merges a subagent spec's
own `middleware` list and nothing else, so a spec that omits it spends
completely unmetered against the shared ceiling. budget_guard.py's docstring
says exactly this, and deep_agent.py declares its own general-purpose spec
specifically to close it -- because `create_deep_agent` auto-adds one
otherwise, carrying the coordinator's tools and model but none of its custom
middleware.

agent/planning_chat.py is the case that proved the hazard real: it declared no
subagents at all, got the auto-added one anyway, and a turn on 2026-08-27
recorded $0.44 against $3.98 of actual router spend because every nested model
call was invisible to the tracker. These tests lock both halves -- build tasks
must govern the subagents they intend to have, and the planning chat must not
have any.
"""

from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

import agent.deep_agent as da
import agent.planning_chat as pc
from agent.config import load_config
from agent.middleware.budget_guard import BudgetGuardMiddleware
from agent.middleware.hidden_tools import HiddenToolsMiddleware


def _capture(monkeypatch, module):
    """Intercept the create_deep_agent call and keep its kwargs."""
    captured = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(module, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(module, "llm_for_role", lambda *a, **k: FakeListChatModel(responses=["x"]))
    return captured


@pytest.fixture
def build_args(tmp_path, monkeypatch):
    projects = {"demo": {"sandbox": str(tmp_path)}}
    monkeypatch.setattr(da, "PROJECTS", projects, raising=False)
    monkeypatch.setattr(pc, "PROJECTS", projects, raising=False)
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", projects)
    return load_config(), "demo", InMemorySaver(), InMemoryStore()


def _kinds(spec) -> set:
    return {type(m) for m in spec.get("middleware", [])}


# ---------------------------------------------------------------------------
# build tasks: subagents are intended, so they must be governed
# ---------------------------------------------------------------------------


async def test_every_build_subagent_carries_the_budget_guard(build_args, monkeypatch):
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    subagents = captured.get("subagents") or []
    assert subagents, "build_deep_agent declared no subagents -- the auto-added one is ungoverned"
    for spec in subagents:
        assert BudgetGuardMiddleware in _kinds(spec), f"{spec['name']} spends unmetered"


async def test_every_build_subagent_carries_the_call_limits(build_args, monkeypatch):
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    for spec in captured["subagents"]:
        kinds = _kinds(spec)
        assert ModelCallLimitMiddleware in kinds, f"{spec['name']} has no model-call backstop"
        assert ToolCallLimitMiddleware in kinds, f"{spec['name']} has no tool-call backstop"


async def test_all_build_subagents_share_the_coordinators_tracker(build_args, monkeypatch):
    """Per-instance trackers would make the real ceiling budget * (1+n)."""
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    trackers = {id(m.tracker)
                for spec in captured["subagents"]
                for m in spec["middleware"] if isinstance(m, BudgetGuardMiddleware)}
    trackers |= {id(m.tracker) for m in captured["middleware"] if isinstance(m, BudgetGuardMiddleware)}
    assert len(trackers) == 1, f"{len(trackers)} separate trackers -- the ceiling is not aggregate"


async def test_build_declares_a_general_purpose_spec_to_suppress_the_auto_add(build_args, monkeypatch):
    """graph.py suppresses its auto-add by NAME, so the name must match."""
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    assert "general-purpose" in {s["name"] for s in captured["subagents"]}


# ---------------------------------------------------------------------------
# planning: no subagents at all
# ---------------------------------------------------------------------------


async def test_planning_declares_no_subagents(build_args, monkeypatch):
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    assert not captured.get("subagents")


async def test_planning_hides_the_auto_added_task_tool(build_args, monkeypatch):
    """Declaring none is not enough -- create_deep_agent adds one regardless,
    so the tool has to be withheld from the model explicitly."""
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    hiders = [m for m in captured["middleware"] if isinstance(m, HiddenToolsMiddleware)]
    assert hiders, "nothing withholds `task` from the planning model"
    assert "task" in set().union(*(h.hidden for h in hiders))


async def test_planning_still_meters_its_own_spend(build_args, monkeypatch):
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, repo, cp, store)
    assert any(isinstance(m, BudgetGuardMiddleware) for m in captured["middleware"])


# ---------------------------------------------------------------------------
# scratch-search trap removal -- build agents, same as planning
# ---------------------------------------------------------------------------
#
# Live 2026-08-27 (screenshot from the operator): the build coder looped
# grep('noFetch'/'skipFetch'/'repairInterior...') against repo paths and got
# "No matches found" every time -- built-in grep searches the memory/skills
# space, never the repo, and those strings DO exist in the repo. A build that
# believes those results concludes the code it must fix is absent. bash rg
# covers repo search strictly better; read_file/ls still cover skills/memory.


def _hidden_by(spec_or_list):
    mws = spec_or_list.get("middleware", []) if isinstance(spec_or_list, dict) else spec_or_list
    hidden = set()
    for m in mws:
        if isinstance(m, HiddenToolsMiddleware):
            hidden |= set(m.hidden)
    return hidden


async def test_every_build_subagent_hides_the_scratch_search_tools(build_args, monkeypatch):
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    for spec in captured["subagents"]:
        hidden = _hidden_by(spec)
        for trap in ("glob", "grep", "execute"):
            assert trap in hidden, f"{spec['name']} still offers {trap!r}"


async def test_the_build_coordinator_hides_them_too(build_args, monkeypatch):
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    hidden = _hidden_by(captured["middleware"])
    for trap in ("glob", "grep", "execute"):
        assert trap in hidden, f"coordinator still offers {trap!r}"


async def test_build_agents_do_not_hide_task_or_the_memory_tools(build_args, monkeypatch):
    """Build agents legitimately delegate (task) and read/write their memory
    and skills (read_file/ls/write_file/edit_file) -- hiding those would be
    over-correction, not trap removal."""
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    hidden = _hidden_by(captured["middleware"])
    for spec in captured["subagents"]:
        # The verifier writes nothing but probe scripts, through bash into
        # .scratch/; its memory write tools confused it (2026-09-24), so they
        # are hidden from that seat alone.
        if spec.get("name") == "verifier":
            assert {"write_file", "edit_file"} <= _hidden_by(spec)
            continue
        hidden |= _hidden_by(spec)
    for keep in ("task", "read_file", "ls", "write_file", "edit_file"):
        assert keep not in hidden, f"{keep!r} must stay visible to build agents"


async def test_planning_turns_have_a_real_dollar_ceiling(build_args, monkeypatch):
    """Planning ran with budget_usd=math.inf -- the one agent with no
    ceiling was the one the operator watched spend $7 on a single turn
    (2026-08-28: 157 calls, 10.6M input tokens). Each turn now gets a fixed
    allowance on top of whatever the session already spent."""
    import math
    cfg, repo, cp, store = build_args
    _capture(monkeypatch, pc)
    _, _, tracker = await pc.build_planning_agent(cfg, repo, cp, store, starting_cost=1.25)
    assert math.isfinite(tracker.budget_usd), "planning budget is still infinite"
    # The ceiling is operator-tunable now (Settings -> Runtime limits), so
    # assert against the live value rather than a constant -- the guard is
    # that a ceiling EXISTS and is applied on top of prior spend, not that it
    # happens to be $4.
    from agent import runtime_settings as rs
    assert tracker.budget_usd == 1.25 + rs.value("planning_turn_budget_usd")
    assert tracker.total_cost == 1.25


# ---------------------------------------------------------------------------
# frontend route: the coordinator and investigator move to the frontend coder
# alias; the test-writer and the todo planner stay put (operator's call,
# 2026-09-09).
# ---------------------------------------------------------------------------


async def _roles_requested(monkeypatch, build_args, route):
    roles = []

    def recording_llm_for_role(config, role, **kwargs):
        roles.append(role)
        return FakeListChatModel(responses=["x"])

    monkeypatch.setattr(da, "create_deep_agent", lambda **kwargs: MagicMock())
    monkeypatch.setattr(da, "llm_for_role", recording_llm_for_role)
    config, repo, checkpointer, store = build_args
    await da.build_deep_agent(config, repo, budget_usd=1.0, checkpointer=checkpointer, store=store, route=route)
    return roles


async def test_general_route_keeps_every_seat(monkeypatch, build_args):
    roles = await _roles_requested(monkeypatch, build_args, "general")
    for r in ("agent-coder", "agent-planner", "agent-investigator", "agent-test-writer"):
        assert r in roles
    assert "agent-coder-frontend" not in roles


async def test_frontend_route_moves_coder_and_investigator_only(monkeypatch, build_args):
    roles = await _roles_requested(monkeypatch, build_args, "frontend")
    assert "agent-coder-frontend" in roles
    assert "agent-coder" not in roles and "agent-investigator" not in roles
    assert "agent-test-writer" in roles and "agent-planner" in roles


async def test_no_agent_carries_two_instances_of_one_middleware(build_args, monkeypatch):
    """LangChain's create_agent refuses duplicate middleware instances. A second
    ToolCallLimitMiddleware on the verifier (2026-09-25) passed every test --
    they capture the build rather than run it -- and stopped every task at its
    first step. Checked here on what the build hands to create_deep_agent."""
    cfg, repo, cp, store = build_args
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, repo, 5.0, cp, store)
    seats = [("coordinator", captured["middleware"])] + [(s["name"], s.get("middleware") or [])
                                                         for s in captured["subagents"]]
    for name, mws in seats:
        kinds = [type(m).__name__ for m in mws]
        dupes = {k for k in kinds if kinds.count(k) > 1}
        assert not dupes, f"{name} carries more than one {sorted(dupes)}"
