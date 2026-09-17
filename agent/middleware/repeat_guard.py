"""RepeatCallGuardMiddleware -- the same tool call, with the same result, is
not run a third time.

Why this exists
---------------
2026-09-09, a Kimi frontend build: the coder issued one bash command --
itself a degenerate string, the same grep repeated with `echo ===` between
-- FOURTEEN times in a row, each returning byte-identical output, before it
noticed and moved on. Fourteen model calls at 50k tokens of context each,
for nothing. The per-file read cap and the planner's search repeat guard
catch their own tools; plain bash had nothing, and neither did edit, write
or anything else.

This is the generic version, at the one seam every tool call passes
through (`wrap_tool_call`). It keys on the tool name plus its arguments and
remembers the last result per key:

- The FIRST and SECOND identical calls run normally (a re-run after an
  intervening change is legitimate, and the second confirms the result is
  stable).
- The THIRD identical call whose two predecessors returned the same result
  is not executed: the model gets the cached result back with a note that
  nothing changed and that the call will not be repeated. That is the
  moment a loop becomes visible to the model, in its own tool result.
- From the FOURTH on it gets a refusal naming the change to make.

A call whose previous result DIFFERED from the one before (a flaky test, a
poll) is never blocked: the guard is for calls that have already proven
they will answer the same way. Non-idempotent tools by nature (write_todos,
save_plan, ask_user) are exempt by name -- re-running them is the point.
"""

from __future__ import annotations

import hashlib
import json

from langchain_core.messages import ToolMessage
from langchain.agents.middleware.types import AgentMiddleware

# Tools whose repeat is meaningful, not a loop.
DEFAULT_EXEMPT = frozenset({"write_todos", "save_plan", "save_brief", "create_project", "ask_user", "task", "describe_image"})
CACHED_AT = 3      # the Nth identical call is answered from cache
REFUSED_AT = 4     # and from here on refused
# A model that keeps issuing the SAME refused call is no longer steering:
# each refusal is still a model call, and on 2026-09-09 a Kimi coder ran
# through forty of them in a row ($0.04 each) after the guard had stopped
# executing anything. Past this many consecutive refusals the pass ends with
# RepeatLoopError -- the work node escalates with the reason, the operator
# resumes on a different seat.
BREAK_AT = 8


class RepeatLoopError(RuntimeError):
    """Raised by the guard when a model repeats a refused call BREAK_AT times."""
_RESULT_PREVIEW = 1_200


def _key(tool_call: dict) -> str:
    name = tool_call.get("name", "")
    try:
        args = json.dumps(tool_call.get("args") or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = repr(tool_call.get("args"))
    return hashlib.sha1(f"{name}\n{args}".encode()).hexdigest()


def _result_text(result) -> str:
    if isinstance(result, ToolMessage):
        c = result.content
        return c if isinstance(c, str) else json.dumps(c, default=str)
    return repr(result)


class RepeatCallGuardMiddleware(AgentMiddleware):
    def __init__(self, exempt: frozenset[str] = DEFAULT_EXEMPT):
        super().__init__()
        self.exempt = exempt
        # key -> {"n": consecutive identical calls, "results": [hash, ...], "last": ToolMessage}
        self._runs: dict[str, dict] = {}
        self._last_key: str | None = None

    # -- bookkeeping ----------------------------------------------------------
    def _before(self, request):
        tool_call = request.tool_call
        name = tool_call.get("name", "")
        if name in self.exempt:
            self._last_key = None
            return None
        key = _key(tool_call)
        if key != self._last_key:
            self._runs.pop(self._last_key, None)
            self._runs.setdefault(key, {"n": 0, "results": [], "last": None})
        self._last_key = key
        run = self._runs.setdefault(key, {"n": 0, "results": [], "last": None})
        run["n"] += 1
        # Only calls that have proven stable are blocked: the previous two
        # results must match each other.
        stable = len(run["results"]) >= 2 and run["results"][-1] == run["results"][-2]
        if run["n"] >= REFUSED_AT and stable:
            if run["n"] >= REFUSED_AT + BREAK_AT:
                raise RepeatLoopError(
                    f"stuck in a tool loop: `{tool_call.get('name')}` with identical arguments requested "
                    f"{run['n']} times in a row with an unchanging result, {BREAK_AT} of them after the guard "
                    f"refused to run it. The model is no longer steering; ending this pass so the task can be "
                    f"resumed on a different seat."
                )
            return self._refusal(tool_call, run)
        if run["n"] >= CACHED_AT and stable and run["last"] is not None:
            return self._cached(tool_call, run)
        return None

    def _after(self, request, result):
        key = self._last_key
        if key is None or key not in self._runs:
            return result
        run = self._runs[key]
        run["results"] = (run["results"] + [hashlib.sha1(_result_text(result).encode()).hexdigest()])[-2:]
        run["last"] = result
        return result

    def _cached(self, tool_call, run) -> ToolMessage:
        preview = _result_text(run["last"])[:_RESULT_PREVIEW]
        return ToolMessage(
            content=(
                f"REPEATED CALL: this is the {run['n']}th identical `{tool_call.get('name')}` call in a row and the "
                f"last two returned exactly the same result, so it was not run again. That result:\n{preview}\n\n"
                f"Nothing about the repo changed between those calls. Do something different: change the "
                f"arguments, act on this result, or state what it tells you and move on."
            ),
            tool_call_id=tool_call["id"],
            status="error",
        )

    def _refusal(self, tool_call, run) -> ToolMessage:
        return ToolMessage(
            content=(
                f"ERROR: `{tool_call.get('name')}` with these exact arguments has now been requested {run['n']} times "
                f"in a row with an unchanging result. It will not run again with these arguments. You are in a "
                f"loop: stop, write down what the last result told you, and take a DIFFERENT next step "
                f"(different command or file, an edit, a check, or finish the todo)."
            ),
            tool_call_id=tool_call["id"],
            status="error",
        )

    # -- the seam ------------------------------------------------------------
    def wrap_tool_call(self, request, handler):
        short = self._before(request)
        if short is not None:
            return short
        return self._after(request, handler(request))

    async def awrap_tool_call(self, request, handler):
        short = self._before(request)
        if short is not None:
            return short
        return self._after(request, await handler(request))
