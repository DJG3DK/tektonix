"""StepBackMiddleware -- a task that has spent a lot without converging is
asked to stop and restate what it is doing, once at each checkpoint.

Why this exists
---------------
2026-09-24, SWE-bench sample: the tasks that failed were the expensive ones.
Two ran over two hours and $1.80 each, rerunning tests two dozen times,
without their fix getting closer to the issue. Nothing asked them to look up
from the loop: the repeat guard only catches the SAME call, and these were
all different calls going in a circle.

At a third and two-thirds of the task's budget, and at 45 and 90 minutes into
a pass, the next model call gets a short checkpoint appended: restate what
should happen, what the change does, and what evidence shows it works; if
that evidence has not moved, take the simplest change consistent with the
request, verify it, and finish. Appended to that one call (like
StaleTodoMiddleware), so the cached prompt prefix is untouched, and each
checkpoint fires once. Coordinator only.
"""

from __future__ import annotations

import time

from langchain_core.messages import HumanMessage
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse

BUDGET_FRACTIONS = (1 / 3, 2 / 3)
MINUTES = (45, 90)


def render_checkpoint(spent: float, budget: float, minutes: float) -> str:
    return (
        "=== CHECKPOINT ===\n"
        f"This task has now spent ${spent:.2f} of its ${budget:.2f} budget"
        + (f", {minutes:.0f} minutes into this pass" if minutes >= 1 else "") + ".\n"
        "Before your next step, write three short lines:\n"
        "1. What the request says should happen (the behaviour, in its own terms).\n"
        "2. What your change does now.\n"
        "3. What evidence shows it works: which runs, on which inputs, with what result.\n"
        "If the last several steps have not changed line 3, you are going in circles. Pick the "
        "simplest change consistent with line 1, verify it on the reported case and its neighbours, "
        "and finish. Do not start a new line of investigation unless line 3 shows a concrete failure.\n"
        "=== END CHECKPOINT ==="
    )


class StepBackMiddleware(AgentMiddleware):
    def __init__(self, tracker, budget_fractions=BUDGET_FRACTIONS, minutes=MINUTES, clock=time.monotonic):
        super().__init__()
        self.tracker = tracker
        self.clock = clock
        self.started = clock()
        budget = float(getattr(tracker, "budget_usd", 0) or 0)
        spent = float(getattr(tracker, "total_cost", 0) or 0)
        # A checkpoint the task had already passed before this pass began
        # fired then (or would have); it is not repeated on every resume.
        self._cost_marks = [f * budget for f in budget_fractions if budget and f * budget > spent]
        self._time_marks = list(minutes)

    def _due(self) -> bool:
        due = False
        spent = float(getattr(self.tracker, "total_cost", 0) or 0)
        while self._cost_marks and spent >= self._cost_marks[0]:
            self._cost_marks.pop(0)
            due = True
        elapsed = (self.clock() - self.started) / 60
        while self._time_marks and elapsed >= self._time_marks[0]:
            self._time_marks.pop(0)
            due = True
        return due

    def _augment(self, request: ModelRequest) -> ModelRequest:
        if not self._due():
            return request
        block = render_checkpoint(float(getattr(self.tracker, "total_cost", 0) or 0),
                                  float(getattr(self.tracker, "budget_usd", 0) or 0),
                                  (self.clock() - self.started) / 60)
        return request.override(messages=[*request.messages, HumanMessage(content=block)])

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return handler(self._augment(request))

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        return await handler(self._augment(request))
