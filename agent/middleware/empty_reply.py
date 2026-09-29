"""EmptyReplyRetryMiddleware -- a reply that is nothing but exhausted
reasoning is retried before the conversation sees it: first on the same
seat with reasoning off, then on the fallback seat.

Why this exists
---------------
2026-09-25, a SWE-bench run: the coder seat returned an AIMessage with empty
content and no tool calls FIFTY times, each with finish_reason "length" and
32768 completion tokens -- every one of them a reasoning token. Each cost
about $0.04 and 75-240 s and moved the task nowhere. The work node's nudge
("your last reply was empty -- carry on") re-asked the SAME model, which
thought itself into the cap again, three in a row. A live test the same day
showed that model ignores the router's `reasoning.max_tokens` and `effort`
parameters, so the cap cannot be lowered for it; the other seats honour
`reasoning_effort="low"`.

So: at the one seam every model call passes through (`wrap_model_call`),
a reply that is empty AND length-capped (or empty with a completion count
only a runaway chain of thought produces) is sent again, once, to the
fallback model, with a note asking for the next step without re-deriving
everything. The note is only in the retry request, never persisted. If the
fallback is empty too the reply goes through unchanged and the work node's
nudge (EMPTY_REPLY_RETRIES, twice per pass) remains the last resort. At
most MAX_RETRIES_PER_INVOCATION retries per agent invocation, reset in
before_agent; the count is per middleware instance, so parallel `task`
calls of one seat type share it, and it is approximate for them.

A retry that the budget guard refuses is not a failed retry: the ceiling
is the answer, and it propagates (2026-09-29 audit, A3: it was logged and
the conversation ran on past its budget). On the coordinator it sits INSIDE PlanCodeModelMiddleware,
which sets the model on every call and would otherwise replace the retry's
fallback model with its own pick.
"""

from __future__ import annotations

import logging
import re

from langchain_core.messages import AIMessage, HumanMessage

from agent.harness_voice import harness
from agent.middleware.budget_guard import BudgetExceededError
from langchain.agents.middleware.types import AgentMiddleware

logger = logging.getLogger("tektonix")

# finish_reason is not always reported through the router; a completion this
# long with nothing to show for it is the same runaway reasoning.
RUNAWAY_COMPLETION_TOKENS = 16_000
# Per agent invocation (a coordinator pass, a subagent's `task` call). A
# bound, not a budget: one coordinator emptied five times in its first hour
# and every retry on the fallback answered (2026-09-25); past it the work
# node's nudge takes over, which re-asks the model that just emptied.
MAX_RETRIES_PER_INVOCATION = 30

# What a provider says when `reasoning.enabled: false` is not on offer.
CANNOT_DISABLE_REASONING = re.compile(r"reasoning is mandatory|cannot be disabled|reasoning.*(required|must be enabled)", re.I)
RETRY_NOTE = ("Your previous attempt ran out of output tokens while thinking and produced nothing. "
              "Answer directly: the next tool call, or your conclusion, without re-deriving everything.")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _completion_tokens(msg: AIMessage) -> int:
    usage = getattr(msg, "usage_metadata", None) or {}
    n = usage.get("output_tokens") if isinstance(usage, dict) else None
    if not n:
        meta = getattr(msg, "response_metadata", None) or {}
        token_usage = meta.get("token_usage") or meta.get("usage") or {}
        n = token_usage.get("completion_tokens") if isinstance(token_usage, dict) else None
    return int(n or 0)


def _reasoning_tokens(msg: AIMessage) -> int:
    meta = getattr(msg, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    details = token_usage.get("completion_tokens_details") if isinstance(token_usage, dict) else None
    if isinstance(details, dict):
        return int(details.get("reasoning_tokens") or 0)
    usage = getattr(msg, "usage_metadata", None) or {}
    details = usage.get("output_token_details") if isinstance(usage, dict) else None
    return int((details or {}).get("reasoning") or 0)


def _ai_message(response) -> AIMessage | None:
    result = getattr(response, "result", None)
    if result is None and isinstance(response, AIMessage):
        return response
    for msg in reversed(result or []):
        if isinstance(msg, AIMessage):
            return msg
    return None


def is_empty_length_capped(msg: AIMessage | None) -> bool:
    """No text, no tool call, and the generation hit its cap (or ran long
    enough that it must have)."""
    if msg is None:
        return False
    if _text(msg.content).strip() or (getattr(msg, "tool_calls", None) or []):
        return False
    meta = getattr(msg, "response_metadata", None) or {}
    if meta.get("finish_reason") == "length":
        return True
    return _completion_tokens(msg) >= RUNAWAY_COMPLETION_TOKENS


class EmptyReplyRetryMiddleware(AgentMiddleware):
    """`quiet_model` is the same seat with its chain of thought switched off.
    2026-09-28, the first Windows install: the test writer emptied on its
    own pin and then on the fallback seat too, thirty times, for a prompt
    that made both think until the cap. The blowout is pure reasoning, so
    the first retry is the same model told not to reason; the fallback seat
    is the second try. Either answering ends it."""

    def __init__(self, fallback_model, seat: str, quiet_model=None):
        super().__init__()
        self.fallback_model = fallback_model
        self.quiet_model = quiet_model
        self.seat = seat
        self.retries = 0

    def before_agent(self, state, runtime):
        self.retries = 0
        return None

    def _attempts(self):
        """The retries for one call, in order: the seat without reasoning
        when there is such a model, then the fallback seat."""
        out = []
        if self.quiet_model is not None:
            out.append(("the same seat without reasoning", self.quiet_model))
        out.append(("the fallback seat", self.fallback_model))
        return out

    def _retry_request(self, request, msg: AIMessage, where: str, model):
        if self.retries >= MAX_RETRIES_PER_INVOCATION:
            logger.warning("%s: empty length-capped reply again, past %d retries this invocation; "
                           "handing it through", self.seat, MAX_RETRIES_PER_INVOCATION)
            return None
        self.retries += 1
        logger.warning("%s: empty length-capped reply (%d completion tokens, %d reasoning); "
                       "retrying on %s (%d/%d this invocation)",
                       self.seat, _completion_tokens(msg), _reasoning_tokens(msg), where,
                       self.retries, MAX_RETRIES_PER_INVOCATION)
        return request.override(
            model=model,
            messages=list(request.messages) + [HumanMessage(content=harness(RETRY_NOTE))],
        )

    def _retry_failed(self, where: str, model, e: Exception) -> None:
        """A retry that errors is not worse than the empty reply it was for:
        it is dropped, and the next option is tried. A provider that refuses
        to run without reasoning ("Reasoning is mandatory for this endpoint",
        2026-09-28, a coder pinned to such a model: six 502s in a row ended
        the task) is remembered, so this seat is not asked again."""
        text = str(e)
        logger.warning("%s: retry on %s failed: %s", self.seat, where, text[:200])
        if model is self.quiet_model and CANNOT_DISABLE_REASONING.search(text):
            logger.warning("%s: this seat's model cannot run without reasoning; not asking again", self.seat)
            self.quiet_model = None

    def wrap_model_call(self, request, handler):
        response = handler(request)
        for where, model in self._attempts():
            msg = _ai_message(response)
            if not is_empty_length_capped(msg):
                return response
            retry = self._retry_request(request, msg, where, model)
            if retry is None:
                return response
            try:
                response = handler(retry)
            except BudgetExceededError:
                raise
            except Exception as e:  # noqa: BLE001 -- see _retry_failed
                self._retry_failed(where, model, e)
        return response

    async def awrap_model_call(self, request, handler):
        response = await handler(request)
        for where, model in self._attempts():
            msg = _ai_message(response)
            if not is_empty_length_capped(msg):
                return response
            retry = self._retry_request(request, msg, where, model)
            if retry is None:
                return response
            try:
                response = await handler(retry)
            except BudgetExceededError:
                raise
            except Exception as e:  # noqa: BLE001 -- see _retry_failed
                self._retry_failed(where, model, e)
        return response
