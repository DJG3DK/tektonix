"""The planning brief: save_brief pins the request, BriefFirstMiddleware
withholds every other tool until it exists, PinnedBriefMiddleware carries it
in the system message so compaction cannot drop it, and match_skills routes
the request to the architecture skills that cover it."""

from langchain_core.messages import SystemMessage
from langchain.agents.middleware.types import ModelRequest

from agent.middleware.pinned_brief import BriefFirstMiddleware, PinnedBriefMiddleware, render_brief
from agent.tools.planning_tools import make_planning_tools, match_skills


class _Tool:
    def __init__(self, name):
        self.name = name


class _Req:
    """The two ModelRequest fields the middlewares touch, with override()."""

    def __init__(self, tools, system_message=None):
        self.tools = tools
        self.system_message = system_message

    def override(self, **kw):
        r = _Req(kw.get("tools", self.tools), kw.get("system_message", self.system_message))
        return r


MANIFEST = {
    "codebase-map": "Structural map of the webapp codebase.",
    "gridder-architecture": "How a project's grid strategy works -- signal level sources ... Read before touching gridder.js, levelsModule.js.",
    "trendsignal-architecture": "How webapp's trend-signal strategy works -- ... Read before touching trendSignal.js, levelsModule.js.",
    "cta-architecture": "How webapp's CTA trend-following strategy works -- MA ensemble ... Read before touching ctaTrend.js, ctaCore.js.",
    "bybit-perp-mechanics": "How Bybit USDT perpetuals settle -- funding sign/timing ... Read before touching bybitClient.js.",
}


def test_match_skills_routes_a_request_to_the_skill_that_covers_it():
    matched = match_skills("a hard plan for the trendSignal strategy: fewer false entries", MANIFEST)
    assert matched[0] == "trendsignal-architecture"
    assert "codebase-map" not in matched, "the map is already mandated by the prompt"


def test_match_skills_needs_more_than_a_filler_word_in_common():
    assert match_skills("make the dashboard faster", MANIFEST) == []


def test_match_skills_finds_a_skill_by_its_camel_case_file_name():
    assert "cta-architecture" in match_skills("why does ctaTrend.js throttle itself on 4h?", MANIFEST)


def test_save_brief_pins_and_reports_matching_skills(monkeypatch):
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", {})
    tools, plan_ref = make_planning_tools(skills_manifest=MANIFEST)
    save_brief = {t.name: t for t in tools}["save_brief"]
    assert plan_ref["brief"] is None

    reply = save_brief.invoke({
        "goal": "cut trendSignal false entries without losing trade count",
        "deliverable": "a build-ready plan",
        "out_of_scope": "STRATA and CTA",
        "needs": "trendSignal.js, its backtester",
    })

    assert plan_ref["brief"]["goal"].startswith("cut trendSignal")
    assert plan_ref["brief"]["matched_skills"][0] == "trendsignal-architecture"
    assert "/skills/trendsignal-architecture/SKILL.md" in reply
    assert "BEFORE any list_project_dir/read_project_file" in reply


def test_save_brief_without_a_match_points_at_the_map(monkeypatch):
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", {})
    tools, plan_ref = make_planning_tools(skills_manifest=MANIFEST)
    reply = {t.name: t for t in tools}["save_brief"].invoke({"goal": "rename things", "deliverable": "a plan"})
    assert plan_ref["brief"]["matched_skills"] == []
    assert "codebase-map" in reply


def test_existing_brief_is_seeded_into_plan_ref(monkeypatch):
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", {})
    brief = {"goal": "g", "deliverable": "d"}
    _, plan_ref = make_planning_tools(existing_brief=brief)
    assert plan_ref["brief"] is brief


async def test_brief_first_hides_everything_but_save_brief_until_a_brief_exists():
    ref = {"brief": None}
    mw = BriefFirstMiddleware(ref)
    tools = [_Tool(n) for n in ("read_project_file", "save_brief", "describe_image", "save_plan", "web_search")]
    seen = []

    async def handler(req):
        seen.append([t.name for t in req.tools])
        return "ok"

    await mw.awrap_model_call(_Req(tools), handler)
    assert set(seen[-1]) == {"save_brief", "describe_image"}

    ref["brief"] = {"goal": "g", "deliverable": "d"}
    await mw.awrap_model_call(_Req(tools), handler)
    assert seen[-1] == [t.name for t in tools], "once the brief exists, every tool is back"


async def test_pinned_brief_is_appended_to_the_system_message_on_every_call():
    ref = {"brief": {"goal": "cut false entries", "deliverable": "a plan", "out_of_scope": "CTA",
                     "needs": "trendSignal.js", "matched_skills": ["trendsignal-architecture"]}}
    mw = PinnedBriefMiddleware(ref)
    seen = []

    async def handler(req):
        seen.append(req.system_message.text)
        return "ok"

    await mw.awrap_model_call(_Req([], SystemMessage("You are a planning assistant.")), handler)
    prompt = seen[-1]
    assert prompt.startswith("You are a planning assistant.")
    assert "PINNED BRIEF" in prompt and "GOAL: cut false entries" in prompt
    assert "OUT OF SCOPE: CTA" in prompt
    assert "/skills/trendsignal-architecture/SKILL.md" in prompt
    assert "call save_brief again" in prompt


async def test_pinned_brief_leaves_the_request_alone_when_there_is_no_brief():
    mw = PinnedBriefMiddleware({"brief": None})
    seen = []

    async def handler(req):
        seen.append(req)
        return "ok"

    req = _Req([], SystemMessage("base"))
    await mw.awrap_model_call(req, handler)
    assert seen[-1] is req


def test_render_brief_is_empty_for_no_brief():
    assert render_brief(None) == "" and render_brief({}) == ""


def test_real_model_request_accepts_the_override_the_middlewares_use():
    """Guard against the langchain ModelRequest API drifting under us."""
    fields = ModelRequest.__dataclass_fields__ if hasattr(ModelRequest, "__dataclass_fields__") else {}
    assert "system_message" in fields and "tools" in fields
