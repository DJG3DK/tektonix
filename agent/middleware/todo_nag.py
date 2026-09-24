"""StaleTodoMiddleware -- the coordinator's own plan is kept current while it
works, not ticked off in one sweep at the end.

Why this exists
---------------
2026-09-11, task 4467864c ("clear the remaining CodeQL inbox", 12 items):
two hours into the pass, deep into the ninth item and already delegating
test work to the test-writer subagent, the todo list still read one
`in_progress` and eleven `pending`. The operator watched a step counter sit
at 0/12 for the whole run and only snap to 12/12 at the very end.

Nothing in the pipe was broken. work_node emits a `todos` custom event the
moment the list changes, server.py mirrors it into task meta and publishes
it, and the frontend's PlanTracker counts `done`. The list simply never
changed: the model wrote it once, marked item one in_progress as
instructed, and then did not call `write_todos` again until something made
it. TodoListMiddleware's own tool description already says to mark items
complete immediately and not to batch completions -- advisory text a model
deep in a long tool loop stops re-reading.

The end-of-run sweep the operator sees is verify_and_ship's incomplete-plan
nudge (`_unfinished_todos`): checks pass, a real diff exists, the plan still
looks unfinished, so the commit is held and the pass loops back telling the
model to tick off what it has done. That works -- it is why the counter ever
reaches 12/12 -- but it arrives at the end, costs a whole extra work pass,
and gives the operator no progress signal in the meantime.

This middleware puts the same reminder where it is cheap: appended to the
system message of the model call itself, the way PinnedBriefMiddleware pins
the brief, and only once every NAG_EVERY turns that pass without the list
moving. It never blocks a call, never rewrites the list itself, and says
nothing at all when the list is absent, freshly changed, or fully ticked --
so a model that maintains its plan properly never sees a word of it.

Coordinator only: it is registered alongside TodoListMiddleware in
build_deep_agent's own middleware list, and the subagents' specs carry their
own lists. Their todo lists are private to their runs and are not what the
plan strip renders.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from agent.harness_voice import HARNESS
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse

# Model turns the list may go unchanged before the first reminder, and the
# spacing of every reminder after it. Sized to be quiet: a single todo item
# routinely takes a dozen-plus tool calls (read, edit, run checks, read the
# failure, edit again), so a smaller number would nag a model that is working
# normally through one step.
NAG_EVERY = 20


def render_nag(todos: list[dict], turns: int) -> str:
    """The appended block. Deterministic so a test can assert on it and a
    reader of the raw prompt can find it. Empty when there is nothing to say.
    """
    outstanding = [t for t in todos if isinstance(t, dict) and t.get("status") != "completed"]
    if not outstanding:
        return ""
    nxt = outstanding[0].get("content", "").strip()
    return (
        f"{HARNESS} === PLAN CHECK ===\n"
        f"Your todo list has not changed in {turns} turns and still shows "
        f"{len(outstanding)} item(s) outstanding. The next one is: {nxt}\n"
        "If you have finished anything on that list, call `write_todos` NOW to mark it "
        "completed and to mark what you are working on as in_progress. "
        "`write_todos` REPLACES the whole list, so send every item -- the finished ones "
        "marked completed -- not just the ones that are left. A list containing only the "
        "remaining work reads as a plan where nothing has been done. "
        "That list is the only "
        "progress the operator can see while you work, and the commit gate reads it too -- an "
        "item left un-ticked reads as outstanding no matter how much work you actually did. "
        "If an item turned out to be unnecessary or out of scope, mark it completed and say so "
        "rather than leaving it hanging. If you genuinely have not finished anything since the "
        "last update, ignore this and carry on.\n"
        "=== END PLAN CHECK ==="
    )


class StaleTodoMiddleware(AgentMiddleware):
    """Reminds the coordinator to update a todo list it has stopped touching."""

    def __init__(self, every: int = NAG_EVERY):
        super().__init__()
        self.every = max(1, int(every))
        self._last: list | None = None   # the list as of the turn it last changed
        self._turns = 0                  # model turns since then

    def _augment(self, request: ModelRequest) -> ModelRequest:
        state = request.state or {}
        todos = state.get("todos") or []
        if not todos:
            # No plan yet (or the model cleared it) -- nothing to be stale.
            self._last, self._turns = None, 0
            return request

        if todos != self._last:
            self._last, self._turns = todos, 0
            return request

        self._turns += 1
        # Reminds at `every`, then again every `every` turns after -- not on
        # every turn once the threshold is crossed, which would be chatter in
        # the one situation where the model is already being asked to fix it.
        if self._turns % self.every:
            return request

        block = render_nag(todos, self._turns)
        if not block:
            return request
        # Appended to the END of the conversation, never to the system prompt.
        #
        # It used to be concatenated onto the system message, which is the one
        # thing that must not change between calls: prompt caching keys on a
        # byte-identical prefix, so editing the first bytes of the request
        # invalidated the whole cached context for that turn. At 70-80k tokens
        # of prefix that is the most expensive call of the run, and it fired
        # precisely when the model was already struggling.
        #
        # As a trailing message it is the last thing the model reads -- which
        # is where a reminder belongs anyway -- and every byte before it is
        # still the prefix the provider cached.
        return request.override(messages=[*request.messages, HumanMessage(content=block)])

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return handler(self._augment(request))

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return await handler(self._augment(request))
