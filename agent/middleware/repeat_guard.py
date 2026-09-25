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

Results are compared after stripping the noise that changes between two
runs of the same command (addresses, durations, timestamps, temp paths),
and past HARD_REPEAT_AT identical calls the result is not consulted at all.
"""

from __future__ import annotations

import hashlib
import json
import re

from langchain_core.messages import ToolMessage

from agent.harness_voice import HARNESS
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
# A model sending the same command this many times is looping whatever the
# output says: 2026-09-25, one byte-identical bash command 352 times in a row
# ($1.15, 29 min) whose output carried an object address, so no two results
# ever hashed equal and the guard never fired.
HARD_REPEAT_AT = 8

# What differs between two runs of the same command without meaning
# anything: memory addresses, durations, timestamps, temp paths, pids.
_NOISE = [re.compile(p) for p in (
    r"0x[0-9a-fA-F]+",
    r"\bin \d+(?:\.\d+)?\s*(?:s|ms|sec|secs|seconds?)\b",
    r"\b\d+(?:\.\d+)?\s*(?:ms|secs?|seconds?)\b",
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
    r"\b1[5-9]\d{8}(?:\.\d+)?\b",
    r"/tmp/[^\s'\"`]+",
    r"\bpid=\d+\b",
)]


def _normalise(text: str) -> str:
    for rx in _NOISE:
        text = rx.sub("~", text)
    return text


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# Different calls, the same answer: a probe written to probe_c9.py, then
# probe_c10.py, ... with the same content, 92 times in a row (2026-09-24) --
# never the same arguments, so the per-call count above never moved. Counted
# on results long enough to mean something, not on a short "OK".
SAME_OUTPUT_AT = 8
SAME_OUTPUT_MIN_CHARS = 100


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
    def __init__(self, exempt: frozenset[str] = DEFAULT_EXEMPT, contain: bool = False):
        """`contain` is for a SUBAGENT: when it is stuck, it is ended with a
        report to the coordinator instead of raising, which used to end the
        whole pass and move the entire task to the fallback seat because one
        verifier looped on `git stash` (2026-09-24)."""
        super().__init__()
        self.exempt = exempt
        self.contain = contain
        self._stuck: str | None = None
        self._same = {"hash": None, "key": None, "n": 0}
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
        # results must match each other -- until HARD_REPEAT_AT, past which
        # the result no longer matters.
        stable = len(run["results"]) >= 2 and run["results"][-1] == run["results"][-2]
        hard = run["n"] >= HARD_REPEAT_AT
        if run["n"] >= REFUSED_AT and (stable or hard):
            if run["n"] >= REFUSED_AT + BREAK_AT:
                self._give_up(
                    f"stuck in a tool loop: `{tool_call.get('name')}` with identical arguments requested "
                    f"{run['n']} times in a row, {BREAK_AT} of them after the guard "
                    f"refused to run it. The model is no longer steering")
            return self._refusal(tool_call, run, stable)
        if run["n"] >= CACHED_AT and stable and run["last"] is not None:
            return self._cached(tool_call, run)
        return None

    # The instance lives as long as the agent it is built into, and a
    # subagent's graph is invoked once per task() call: a verifier's round 2
    # started with round 1's counts (2026-09-25). Fresh per invocation.
    def before_agent(self, state, runtime):
        self._stuck = None
        self._same = {"hash": None, "key": None, "n": 0}
        self._runs = {}
        self._last_key = None
        return None

    async def abefore_agent(self, state, runtime):
        return self.before_agent(state, runtime)

    def _give_up(self, why: str) -> None:
        if not self.contain:
            raise RepeatLoopError(f"{why}; ending this pass so the task can be resumed on a different seat.")
        self._stuck = why

    def _same_output(self, request, result):
        """Different calls, identical long output, again and again."""
        text = _result_text(result)
        if len(text) < SAME_OUTPUT_MIN_CHARS:
            self._same = {"hash": None, "key": None, "n": 0}
            return result
        h = hashlib.sha1(text.encode()).hexdigest()
        key = _key(request.tool_call)
        if h == self._same["hash"] and key != self._same["key"]:
            self._same["n"] += 1
        elif h != self._same["hash"]:
            self._same["n"] = 1
        self._same.update(hash=h, key=key)
        n = self._same["n"]
        if n >= SAME_OUTPUT_AT + BREAK_AT:
            self._give_up(f"stuck in a loop: {n} tool calls in a row, with different arguments, returned exactly "
                          f"the same output. The model is no longer steering")
        if n >= SAME_OUTPUT_AT and isinstance(result, ToolMessage):
            note = (f"\n\n{HARNESS} The last {n} calls, each with different arguments, returned exactly this "
                    f"same output. Changing a file name or an argument is not changing anything: stop, say what "
                    f"this output tells you, and take a genuinely different step.")
            return result.model_copy(update={"content": (result.content if isinstance(result.content, str)
                                                         else _result_text(result)) + note})
        return result

    def _after(self, request, result):
        if request.tool_call.get("name", "") not in self.exempt:
            result = self._same_output(request, result)
        key = self._last_key
        if key is None or key not in self._runs:
            return result
        run = self._runs[key]
        run["results"] = (run["results"] + [hashlib.sha1(_normalise(_result_text(result)).encode()).hexdigest()])[-2:]
        run["last"] = result
        return result

    def _cached(self, tool_call, run) -> ToolMessage:
        preview = _result_text(run["last"])[:_RESULT_PREVIEW]
        return ToolMessage(
            content=(
                f"{HARNESS} REPEATED CALL: this is the {_ordinal(run['n'])} identical `{tool_call.get('name')}` call in a row and the "
                f"last two returned exactly the same result, so it was not run again. That result:\n{preview}\n\n"
                f"Nothing about the repo changed between those calls. Do something different: change the "
                f"arguments, act on this result, or state what it tells you and move on."
            ),
            tool_call_id=tool_call["id"],
            status="error",
        )

    def _refusal(self, tool_call, run, stable: bool = True) -> ToolMessage:
        why = ("with an unchanging result" if stable else
               "-- its output differs only in noise (addresses, timings), and nothing you did changed between them")
        return ToolMessage(
            content=(
                f"{HARNESS} ERROR: `{tool_call.get('name')}` with these exact arguments has now been requested {run['n']} times "
                f"in a row {why}. It will not run again with these arguments. You are in a "
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

    # -- a contained subagent that is stuck ends its run --------------------
    def _stopped(self):
        from langchain_core.messages import AIMessage  # noqa: PLC0415
        from langchain.agents.middleware.types import ModelResponse  # noqa: PLC0415
        return ModelResponse(result=[AIMessage(content=(
            f"{HARNESS} This subagent was stopped: {self._stuck}. Its findings up to that point are in the "
            f"conversation above; treat anything it did not report as unchecked."))])

    def wrap_model_call(self, request, handler):
        return self._stopped() if self._stuck else handler(request)

    async def awrap_model_call(self, request, handler):
        return self._stopped() if self._stuck else await handler(request)
