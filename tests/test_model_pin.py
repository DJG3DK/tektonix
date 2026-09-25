"""PlanCodeModelMiddleware's turn classification -- the deterministic rule
that decides which pinned model handles a coordinator turn: planner on a
turn that answers fresh outer input (a HumanMessage after the model's last
own message), coder after."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.middleware.model_pin import is_planning_turn


def test_first_turn_of_a_thread_is_planning():
    assert is_planning_turn([HumanMessage(content="fix the bug")]) is True


def test_mid_loop_tool_turns_are_coding_turns():
    msgs = [HumanMessage(content="fix"), AIMessage(content="plan: ..."),
            ToolMessage(content="ok", tool_call_id="t1")]
    assert is_planning_turn(msgs) is False


def test_fresh_feedback_after_work_is_a_planning_turn():
    """Reviewer findings / checks-failed loopbacks are re-planning moments --
    the first response to any new outer HumanMessage goes to the planner,
    not the coder."""
    msgs = [HumanMessage(content="fix"), AIMessage(content="plan: ..."),
            ToolMessage(content="ok", tool_call_id="t1"),
            HumanMessage(content="reviewer verdict: NEEDS_FIXES ...")]
    assert is_planning_turn(msgs) is True


def test_fresh_generation_thread_replans():
    # A generation-bumped fresh thread starts with only the seeded context
    # HumanMessage -- no AI history -- so it re-plans, which is the point of
    # the restart (see work.py's inner_thread_generation).
    assert is_planning_turn([HumanMessage(content="[fresh context] goal ...")]) is True


def test_empty_and_none_histories_count_as_planning():
    assert is_planning_turn([]) is True
    assert is_planning_turn(None) is True


def test_resume_with_trailing_tool_results_after_operator_message_is_planning():
    """A thread stopped mid-tool-loop resumes by appending the operator's
    message and then the pending tool results -- the human input isn't
    last, but no model response has followed it yet."""
    msgs = [HumanMessage(content="goal"), AIMessage(content="", tool_calls=[{"name": "read", "args": {}, "id": "t1"}]),
            HumanMessage(content="[operator message] continue and finish"),
            ToolMessage(content="file contents", tool_call_id="t1")]
    assert is_planning_turn(msgs) is True
