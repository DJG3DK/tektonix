"""PlanCodeModelMiddleware -- deterministic two-model split for the coordinator.

One pinned model per role rather than an adaptive pool. The coordinator's
work has two distinct shapes, and each is pinned to a different model:

  - PLANNING: the turn that answers fresh OUTER input -- the goal, a
    verify_and_ship loopback, an operator message -- and decides the
    approach. Pinned to a strong general reasoner (agent-planner).
  - CODING: every turn after that -- tool-calling, editing, running checks.
    Pinned to the route's coder alias (agent-coder, or the frontend/fallback
    coder -- agent/frontend_route.py).

The split is deterministic, not classified: see is_planning_turn. It sets
the model on EVERY call, so anything that must override the model (the
empty-reply retry) sits after it in the coordinator's stack.

Coordinator-only -- subagents have their own single pinned models.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse


def is_planning_turn(messages) -> bool:
    """True iff this turn responds directly to fresh OUTER input: a
    HumanMessage arrived after the model's last own message.

    work_node only ever injects HumanMessages at the seams (the goal,
    verify_and_ship feedback, operator messages), so that exactly marks the
    first response to each new piece of outer input -- the planner reads the
    feedback and decides the approach. From the model's first tool call
    onward the last messages are AI/Tool, so execution turns stay on the
    coder. Summarization inserts its summary mid-list, never last, so it
    cannot fake a planning turn. "No AIMessage yet" (only the first turn)
    sent every loopback to the coder; "last message is human" missed a
    thread resumed mid-tool-loop, where the operator's message is followed
    by the interrupted calls' results.
    """
    msgs = [m for m in (messages or []) if not isinstance(m, SystemMessage)]
    if not msgs:
        return True
    for m in reversed(msgs):
        if isinstance(m, HumanMessage):
            return True
        if isinstance(m, AIMessage):
            return False
    return True   # no AI message at all yet -- first turn of the thread


class PlanCodeModelMiddleware(AgentMiddleware):
    def __init__(self, planner_model, coder_model):
        super().__init__()
        self.planner_model = planner_model
        self.coder_model = coder_model

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        model = self.planner_model if is_planning_turn(request.messages) else self.coder_model
        return await handler(request.override(model=model))
