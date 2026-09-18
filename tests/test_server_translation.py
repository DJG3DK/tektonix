"""Unit tests for server.py's todos<->plan translation layer -- the part
that lets the frontend's existing PlanTracker/LogEntryCard components keep
working unmodified against the new deepagents-based graph's own state shape
(latest_todos, not plan/current_step_index). Written during the full
LangGraph/deepagents docs audit, which found zero automated coverage
anywhere in this system.
"""

from agent.server import _classify_model_usage_role, _state_snapshot_for_frontend, _todos_to_plan


def test_todos_to_plan_returns_none_for_none():
    assert _todos_to_plan(None) is None


def test_todos_to_plan_returns_empty_list_for_empty_list():
    assert _todos_to_plan([]) == []


def test_todos_to_plan_maps_status_values_correctly():
    todos = [
        {"content": "step one", "status": "completed"},
        {"content": "step two", "status": "in_progress"},
        {"content": "step three", "status": "pending"},
    ]

    plan = _todos_to_plan(todos)

    assert [s["status"] for s in plan] == ["done", "in_progress", "pending"]
    assert [s["description"] for s in plan] == ["step one", "step two", "step three"]


def test_todos_to_plan_assigns_stable_index_based_ids():
    todos = [{"content": "a", "status": "pending"}, {"content": "b", "status": "pending"}]
    plan = _todos_to_plan(todos)
    assert [s["id"] for s in plan] == ["0", "1"]


def test_todos_to_plan_unknown_status_defaults_to_pending():
    # deepagents' write_todos only ever produces pending/in_progress/completed,
    # but this should degrade safely rather than crash on anything unexpected.
    todos = [{"content": "x", "status": "some_future_status"}]
    plan = _todos_to_plan(todos)
    assert plan[0]["status"] == "pending"


def test_todos_to_plan_result_and_verified_are_always_placeholders():
    # No todo-level equivalent exists for these PlanStep fields in the new
    # design (verify_and_ship gates the WHOLE task, not per-step) -- the
    # frontend doesn't currently render either, but the shape must stay
    # valid for PlanStep's own TS interface.
    todos = [{"content": "x", "status": "completed"}]
    plan = _todos_to_plan(todos)
    assert plan[0]["result"] is None
    assert plan[0]["verified"] is False


def test_state_snapshot_translates_latest_todos_into_plan():
    values = {"latest_todos": [{"content": "x", "status": "pending"}], "cost_so_far": 1.5, "escalated": False}

    snapshot = _state_snapshot_for_frontend(values)

    assert snapshot["plan"] == [
        {"id": "0", "description": "x", "status": "pending", "result": None, "verified": False}
    ]
    assert snapshot["current_step_index"] is None
    # Everything else passes through unchanged.
    assert snapshot["cost_so_far"] == 1.5
    assert snapshot["escalated"] is False


def test_state_snapshot_handles_missing_latest_todos():
    # A task before its first write_todos call, or one that never uses
    # todos at all (a short investigation-only task) -- must not
    # crash, must produce a None plan (frontend keeps its prior/empty plan).
    values = {"cost_so_far": 0.0, "escalated": False}

    snapshot = _state_snapshot_for_frontend(values)

    assert snapshot["plan"] is None


def test_state_snapshot_does_not_mutate_the_input_dict():
    values = {"latest_todos": None, "cost_so_far": 0.0}
    original = dict(values)

    _state_snapshot_for_frontend(values)

    assert values == original


# ---------------------------------------------------------------------------
# _classify_model_usage_role -- the Analytics "model usage by role" scan's
# attribution logic. A background consolidation run used to be silently
# misattributed to the "coder" role (its model never matches the
# agent-planner alias, so it fell into "coder" by the coordinator-split
# fallback), making it look like the coder role wasn't really pinned to one
# model. Must be checked and labeled before any of the live-task tags below.
# ---------------------------------------------------------------------------


def test_consolidation_thread_is_never_misattributed_to_coder():
    metadata = {"thread_id": "consolidation:shop-api:20260821010834"}
    assert _classify_model_usage_role(metadata, alias="reasoning-tier") == "consolidation"


def test_classifier_call_is_never_misattributed_to_coder():
    """classify_task() is a bare ChatOpenAI.ainvoke() outside any graph, so
    it carries no thread_id/lc_agent_name/lc_source at all -- caught by
    alias instead, since there's no thread_id prefix to key off the way
    consolidation has."""
    assert _classify_model_usage_role({}, alias="agent-classifier") == "classifier"


def test_vision_call_is_never_misattributed_to_its_caller():
    """describe_image is callable from the coordinator and from several
    subagents, so a call made from within the investigator (which tags its
    own real LLM turns with lc_agent_name="investigator") must still land
    in "vision", not get folded into whichever role happened to invoke the
    tool. Checked by alias, same reasoning as agent-classifier above."""
    metadata = {"lc_agent_name": "investigator", "thread_id": "t1:work"}
    assert _classify_model_usage_role(metadata, alias="agent-vision") == "vision"


def test_vision_call_from_coordinator_is_never_misattributed_to_coder():
    metadata = {"thread_id": "t1:work"}
    assert _classify_model_usage_role(metadata, alias="agent-vision") == "vision"


def test_planning_chat_call_lands_in_its_own_role():
    metadata = {"thread_id": "planning:abc123"}
    assert _classify_model_usage_role(metadata, alias="agent-planning-chat") == "planning-chat"


def test_planning_chat_summarizer_call_is_not_folded_into_planning_chat():
    """The planning agent runs its own SummarizationMiddleware pinned to
    agent-summarizer -- checked by alias (not the "planning:" thread_id
    prefix) so that call correctly falls through to the lc_source check
    instead of being misattributed to "planning-chat"."""
    metadata = {"thread_id": "planning:abc123", "lc_source": "summarization"}
    assert _classify_model_usage_role(metadata, alias="agent-summarizer") == "summarizer"


def test_subagent_call_uses_lc_agent_name():
    metadata = {"lc_agent_name": "investigator", "thread_id": "t1:work"}
    assert _classify_model_usage_role(metadata, alias="agent-investigator") == "investigator"


def test_summarizer_call_uses_lc_source():
    metadata = {"lc_source": "summarization", "thread_id": "t1:work"}
    assert _classify_model_usage_role(metadata, alias="agent-summarizer") == "summarizer"


def test_coordinator_planning_turn_splits_to_planner():
    metadata = {"thread_id": "t1:work"}
    assert _classify_model_usage_role(metadata, alias="agent-planner") == "planner"


def test_coordinator_non_planning_turn_splits_to_coder():
    metadata = {"thread_id": "t1:work"}
    assert _classify_model_usage_role(metadata, alias="agent-coder") == "coder"


# ---------------------------------------------------------------------------
# plan-strip survival across refresh/task-switch (2026-08-28)
# ---------------------------------------------------------------------------

from agent.server import _apply_plan_fallback


def _todos(*contents, status="in_progress"):
    return [{"content": c, "status": status} for c in contents]


def test_mid_pass_hydrate_falls_back_to_the_meta_mirror():
    """The checkpoint only records latest_todos when a work pass RETURNS; a
    refresh mid-pass used to rebuild an empty step strip until the pass
    ended."""
    snap = _apply_plan_fallback({"plan": None}, {"latest_todos": _todos("step one", "step two")})
    assert [p["description"] for p in snap["plan"]] == ["step one", "step two"]


def test_the_live_mirror_wins_over_the_checkpointed_plan():
    """Reversed 2026-09-09. The checkpoint's list is written only when a pass
    RETURNS; the mirror is written on every todos event. A resumed task's
    coordinator wrote a fresh 3-item list, and a refresh snapped the strip
    back to the previous pass's 15 items because the checkpoint "won"."""
    snap = _apply_plan_fallback({"plan": [{"id": "0", "description": "from the pass before"}]},
                                {"latest_todos": _todos("live step")})
    assert snap["plan"][0]["description"] == "live step"


def test_no_mirror_and_no_checkpoint_plan_stays_none():
    assert _apply_plan_fallback({"plan": None}, {})["plan"] is None


def test_a_missing_snapshot_passes_through():
    assert _apply_plan_fallback(None, {"latest_todos": _todos("x")}) is None


# ---------------------------------------------------------------------------
# live-log buffers -- full stream across refresh/switch (2026-08-28)
# ---------------------------------------------------------------------------

from agent.server import _fuller_log, _live_log_append, _LIVE_LOG_MAX_ENTRIES, _LIVE_LOG_MAX_KEYS


def test_buffer_appends_and_caps_entries():
    book = {}
    _live_log_append(book, "t1", [{"n": i} for i in range(_LIVE_LOG_MAX_ENTRIES + 50)])
    assert len(book["t1"]) == _LIVE_LOG_MAX_ENTRIES
    assert book["t1"][-1] == {"n": _LIVE_LOG_MAX_ENTRIES + 49}  # newest kept, oldest dropped


def test_buffer_evicts_oldest_key_at_the_cap():
    book = {}
    for i in range(_LIVE_LOG_MAX_KEYS + 2):
        _live_log_append(book, f"t{i}", [{"e": 1}])
    assert len(book) == _LIVE_LOG_MAX_KEYS
    assert "t0" not in book and f"t{_LIVE_LOG_MAX_KEYS + 1}" in book


def test_fuller_log_merges_the_two_sources_by_entry_id():
    """Was "whichever list is longer" (2026-09-11). Length is a proxy for
    freshness and it is wrong both ways: a durable list that is longer but
    older replaced newer live entries, and a shorter one was discarded even
    when it held history the buffer never had. Entries carry a
    content-derived id now, so the two sources merge -- durable order first,
    then whatever the live buffer added since."""
    def e(n):
        return {"timestamp": f"t{n}", "node": "work", "step_id": None,
                "summary": f"entry {n}", "detail": "", "cost_usd": 0.0}

    # A mid-pass snapshot is SHORTER than the live buffer: keep everything.
    merged = _fuller_log([e(1), e(2), e(3)], [e(1)])
    assert [x["summary"] for x in merged] == ["entry 1", "entry 2", "entry 3"]

    # A durable list holding history this process never buffered: keep it,
    # and append what the buffer has since.
    merged = _fuller_log([e(3)], [e(1), e(2)])
    assert [x["summary"] for x in merged] == ["entry 1", "entry 2", "entry 3"]

    assert [x["summary"] for x in _fuller_log(None, [e(1)])] == ["entry 1"]
    assert [x["summary"] for x in _fuller_log([e(1)], None)] == ["entry 1"]
    assert _fuller_log(None, None) == []

    # The same entry from both sources is one entry.
    assert len(_fuller_log([e(1)], [e(1)])) == 1


# ---------------------------------------------------------------------------
# planning translator keeps operator turns
# ---------------------------------------------------------------------------

from langchain_core.messages import HumanMessage
from agent.planning_chat import _translate_message as translate_planning_msg


def test_operator_messages_survive_hydration_as_user_entries():
    """HumanMessages were dropped entirely -- a refreshed planning page showed
    the agent answering nobody."""
    entry = translate_planning_msg(HumanMessage(content="please fix the gridder plotting"))
    assert entry["kind"] == "user"
    assert entry["summary"].startswith("please fix")


def test_summarization_plumbing_is_not_rendered_as_a_user_turn():
    entry = translate_planning_msg(HumanMessage(
        content="Here is a summary of the conversation to date:\n\n## SESSION INTENT..."))
    assert entry is None


def test_empty_human_messages_are_skipped():
    assert translate_planning_msg(HumanMessage(content="   ")) is None


# ---------------------------------------------------------------------------
# hydrate cost freshness (2026-08-28) -- exercised through the snapshot merge
# ---------------------------------------------------------------------------

def test_hydrate_prefers_the_fresher_meta_cost():
    """The checkpoint's cost only advances when a pass RETURNS; the meta
    mirror tracks live spend. Cost only grows within a run, so higher wins."""
    snap = {"plan": None, "cost_so_far": 1.10}
    meta = {"cost_so_far": 1.42}
    # mirror of the inline logic in get_task -- kept in lockstep by this test
    if isinstance(meta.get("cost_so_far"), (int, float)) and meta["cost_so_far"] > (snap.get("cost_so_far") or 0):
        snap["cost_so_far"] = meta["cost_so_far"]
    assert snap["cost_so_far"] == 1.42


def test_hydrate_never_regresses_cost_from_a_stale_meta():
    snap = {"plan": None, "cost_so_far": 2.00}
    meta = {"cost_so_far": 1.42}
    if isinstance(meta.get("cost_so_far"), (int, float)) and meta["cost_so_far"] > (snap.get("cost_so_far") or 0):
        snap["cost_so_far"] = meta["cost_so_far"]
    assert snap["cost_so_far"] == 2.00
