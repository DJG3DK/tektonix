"""WrapUpMiddleware -- a bounded subagent is told its budget is running out
while it can still act on it, and is made to report before the cap.

2026-09-25: the verifier's hard cap of 30 tool calls ended one run with
nothing but "Tool call limit reached" -- every finding it had made was lost
and the coordinator started another verifier from scratch. A countdown on
its tool results, before the cap, lets it send its verdict instead.

Same day, the benchmark run: 13 of 48 verifier runs still ended at the cap
with nothing (one had found the exact failing case). The countdown was
keyed on `calls == threshold` and a seat making two calls per turn stepped
over it, and a countdown is advice. So the last model call is not offered
tools at all: the request is overridden with no tools and a harness note
saying to write the report now. The ToolCallLimitMiddleware stays as the
backstop behind it.
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, ToolMessage
from langchain.agents.middleware.types import AgentMiddleware

from agent.harness_voice import HARNESS, harness


class WrapUpMiddleware(AgentMiddleware):
    def __init__(self, limit: int, warn_at: tuple[int, ...] = ()):
        super().__init__()
        self.limit = limit
        self.warn_at = set(warn_at or (max(1, limit - 10), max(1, limit - 4)))
        self.calls = 0
        self._fired: set[int] = set()

    # One instance per agent build, invoked once per task() call: a
    # verifier's round 2 was told "26 of 30 used" after 5 calls (2026-09-25).
    def before_agent(self, state, runtime):
        self.calls = 0
        self._fired = set()
        return None

    async def abefore_agent(self, state, runtime):
        return self.before_agent(state, runtime)

    def _note(self, result):
        self.calls += 1
        # Threshold crossing, not equality: two calls in one turn skip a number.
        crossed = {t for t in self.warn_at if self.calls >= t} - self._fired
        if not crossed or not isinstance(result, ToolMessage):
            return result
        self._fired |= crossed
        left = self.limit - self.calls
        note = (f"\n\n{HARNESS} {self.calls} of your {self.limit} tool calls used, {left} left. "
                + ("Start wrapping up: finish the checks that matter most." if left > 5 else
                   "Send your report NOW, opening with the VERDICT line -- a run cut off at the limit "
                   "returns nothing to the coordinator."))
        content = result.content if isinstance(result.content, str) else str(result.content)
        return result.model_copy(update={"content": content + note})

    def wrap_tool_call(self, request, handler):
        return self._note(handler(request))

    async def awrap_tool_call(self, request, handler):
        return self._note(await handler(request))

    # -- the last model call gets no tools, so the run ends with a report ----
    def _final(self, request):
        if self.calls < self.limit - 1:
            return request
        note = harness(
            f"No more tool calls are available ({self.calls} of {self.limit} used). Write your report "
            "now, as text, based on what you have seen: open with the VERDICT line, then the cases you "
            "checked and what each showed, and say plainly which checks you did not get to.")
        return request.override(tools=[], messages=[*request.messages, HumanMessage(content=note)])

    def wrap_model_call(self, request, handler):
        return handler(self._final(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._final(request))
