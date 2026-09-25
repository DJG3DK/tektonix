"""WrapUpMiddleware -- a bounded subagent is told its budget is running out
while it can still act on it.

2026-09-25: the verifier's hard cap of 30 tool calls ended one run with
nothing but "Tool call limit reached" -- every finding it had made was lost
and the coordinator started another verifier from scratch. A countdown on
its tool results, before the cap, lets it send its verdict instead.
"""
from __future__ import annotations

from langchain_core.messages import ToolMessage
from langchain.agents.middleware.types import AgentMiddleware

from agent.harness_voice import HARNESS


class WrapUpMiddleware(AgentMiddleware):
    def __init__(self, limit: int, warn_at: tuple[int, ...] = ()):
        super().__init__()
        self.limit = limit
        self.warn_at = set(warn_at or (max(1, limit - 10), max(1, limit - 4)))
        self.calls = 0

    def _note(self, result):
        self.calls += 1
        if self.calls not in self.warn_at or not isinstance(result, ToolMessage):
            return result
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
