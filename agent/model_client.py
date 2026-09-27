"""The chat-model client one seat gets: llm_for_role builds it, and every
role in this system -- the build seats in agent/deep_agent.py, the planning
seats in agent/planning_chat.py, the consolidator, the summarizers -- goes
through it, so the router alias, the retry count, the streaming rule and the
ledger metadata are decided in one place.

Nothing here picks WHICH alias a seat runs on: that table is the route ->
seat mapping in build_deep_agent, next to the seats it serves. The names
below are re-exported from agent.deep_agent, which is where the other
modules and the tests import them from.
"""

import os

from langchain_openai import ChatOpenAI

from agent.config import Config
from agent import runtime_settings as _rs


# Output cap for the coordinator and every subagent seat -- see llm_for_role.
# The subagent seats hit the router's 32768 default with content the same
# hour the coordinator's cap went in (13 verifier calls in 30 minutes). The
# empty-reply fallback seat keeps the router's ceiling.
COORDINATOR_MAX_TOKENS = 16384
SEAT_MAX_TOKENS = 16384


def coder_reasoning() -> bool | None:
    """None: the seat's own default. False: the operator turned the coder's
    chain of thought off (CODER_REASONING=off), an experiment from 2026-09-25:
    the coder's losses read as over-thinking ("the canonical upstream shape"),
    its blowouts were pure reasoning, and with reasoning off it answers the
    same prompt in a quarter of the time. The router forwards
    `reasoning.enabled` to the provider; measured to work on the coder's pin."""
    return False if os.environ.get("CODER_REASONING", "").strip().lower() in ("off", "0", "false", "no") else None


def llm_for_role(config: Config, model_name: str, reasoning_effort: str | None = None,
                 timeout: int | None = None, callbacks: list | None = None,
                 task_id: str | None = None, session_id: str | None = None,
                 max_tokens: int | None = None, reasoning: bool | None = None) -> ChatOpenAI:
    # model_name is a bare router alias, resolved entirely by the
    # proxy, not by anything in this process.
    #
    # max_tokens (None by default: the router's own ceiling applies): an
    # output cap for a seat whose blowouts are pure reasoning. The coordinator
    # burned exactly the router's 32768 output tokens with no answer 50 times
    # in one run (2026-09-25); halving the cap halves each blowout, and a
    # real coder answer stays under ~15k (services/model-router/router/upstream.py).
    #
    # stream_usage=True is not optional: ChatOpenAI only auto-enables it when
    # talking to the default OpenAI base_url/client, which this custom
    # router_base_url never matches. Every model call here goes through
    # agent.astream_events(..., version="v3"), so it's invoked as a real
    # token stream rather than a single ainvoke -- and without stream_usage,
    # a streamed OpenAI-compatible response never includes
    # `stream_options: {"include_usage": true}`, so
    # response_metadata["token_usage"] comes back empty on every call.
    # BudgetGuardMiddleware's cost read falls back to 0.0 in that case (by
    # design, to fail loud-ish rather than guess), which would mean the one
    # non-negotiable hard dollar ceiling in this whole system silently never
    # trips.
    #
    # reasoning_effort (None by default -- opt-in per call site, not a
    # blanket default): confirmed live that OpenRouter's Gemini models
    # accept this directly on ChatOpenAI and genuinely spend extra "thinking"
    # tokens on it (response.usage_metadata.output_token_details.reasoning
    # comes back > 0, and it's billed/counted like any other output token --
    # BudgetGuardMiddleware sees it same as always). Not every pinned role's
    # model necessarily supports this OpenRouter parameter; only pass it for
    # a role/model combination confirmed to actually use it.
    #
    # timeout (180 by default, overridable per call site): confirmed live
    # 2026-08-23 that agent-planning-chat (gemini-3.7-flash) called with
    # reasoning_effort="high" routinely blows past 180s -- OpenRouter itself
    # aborts the still-in-flight call ("OpenrouterException - The operation
    # was aborted"), which the router then surfaces to this client as an HTTP
    # 400, which openai's SDK in turn raises as BadRequestError. A "high"
    # reasoning budget on a planning turn isn't on the same latency budget as
    # an interactive coordinator call, so a role that opts into high
    # reasoning_effort should also opt into a longer timeout at its own call
    # site rather than eating spurious aborts on real, in-progress work.
    return ChatOpenAI(
        model=model_name,
        base_url=config.router_base_url,
        api_key=config.router_api_key,
        temperature=0,
        timeout=timeout if timeout is not None else _rs.as_int("model_call_timeout_s"),
        # ONE retry, not the openai SDK's silent default of two.
        #
        # The default was inherited rather than chosen, and it turns a slow
        # call into a very long one without saying so: a tool-calling call
        # (non-streaming, see disable_streaming below) that hits the timeout
        # is retried twice before this code ever sees an error, so one logical
        # model call can occupy 3x the timeout -- fifteen minutes at the
        # current 300s -- while the agent log stays silent, because the SDK
        # swallows the first two failures.
        #
        # Measured 2026-09-14: a coder call logged 1802s upstream at the
        # router for 280 output tokens, err=False, with no corresponding error
        # on this side. The router keeps an abandoned upstream running after
        # the client has given up, so each retry adds a concurrent upstream
        # rather than replacing one.
        #
        # One retry still covers the case retries exist for -- a transient
        # blip -- at half the worst case. Zero would make every hiccup an
        # escalation.
        max_retries=1,
        stream_usage=True,
        # Do not stream a response that carries tool-call arguments.
        #
        # Measured 2026-09-12: one 734-token tool call cost ~25 seconds at
        # 100% of a core, and the whole of it was langchain-core merging the
        # stream back together. Every chunk re-runs AIMessageChunk's
        # init_tool_calls validator, which re-parses the WHOLE accumulated
        # argument JSON, so chunk N pays for chunks 1..N:
        #
        #     25 chunks  0.02s      100 chunks  0.47s
        #     50 chunks  0.09s      200 chunks  2.73s
        #
        # ...while 800 text-only chunks cost 0.009s. The cost is entirely in
        # the tool-call half.
        #
        # Nothing in this system reads those chunks. work.py and
        # planning_chat.py both consume `run.values` -- a full state snapshot
        # per superstep -- so the dashboard shows messages as they complete,
        # never token by token. We were paying tens of CPU-seconds per turn to
        # assemble something no reader ever saw.
        #
        # "tool_calling", not True: a call with no tools bound (the
        # summarizer) still streams, because that path is cheap and harmless.
        # stream_usage above stays for exactly those calls.
        disable_streaming="tool_calling",
        # include_response_headers: the proxy's x-router-call-id lands in
        # response_metadata["headers"], which is how BudgetGuardMiddleware
        # matches a call to the router's own billed cost for it
        # (agent/tools/router_ledger.py). Streaming included: langchain-openai
        # attaches the headers to the first chunk's generation_info, and
        # langchain-core merges generation_info into the final message's
        # response_metadata.
        include_response_headers=True,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        # callbacks: how a model invoked OUTSIDE the graph's model node still
        # gets metered -- SummarizationMiddleware ainvoke()s its summary model
        # directly, where no agent middleware wraps the call, so the summarizer
        # role attaches a BudgetMeterCallback here (see budget_guard.py).
        callbacks=callbacks,
        # Who this call is for, carried to the router so its own ledger can be
        # read back per task.
        #
        # The proxy merges a request body's `metadata` into what its logging
        # callback sees -- the same channel the router's routing_decision
        # already travels on -- so one line of routing.jsonl can say which
        # task spent the money. Without it the ledger can price a CALL (by
        # x-router-call-id) but cannot total a TASK, which is why a restart
        # reset the displayed spend to the last checkpoint and lost everything
        # the killed pass had spent: real money, invisible.
        extra_body={**(_call_metadata(task_id, session_id) or {}),
                    **({"reasoning": {"enabled": False}} if reasoning is False else {})} or None,
    )


def _call_metadata(task_id: str | None, session_id: str | None) -> dict | None:
    tags = {k: v for k, v in (("agent_task_id", task_id), ("agent_session_id", session_id)) if v}
    return {"metadata": tags} if tags else None
