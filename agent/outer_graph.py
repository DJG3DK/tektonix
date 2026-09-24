"""The outer graph: work -> verify_and_ship -> (loop back to work, or terminal).
"""

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, default_retry_on

from agent.config import Config
from agent.graph import open_checkpointer, open_store, project_lock  # noqa: F401 -- re-exported, unchanged
from agent.nodes.verify_and_ship import verify_and_ship_node
from agent.nodes.work import work_node
from agent.outer_state import AgentState, initial_state  # noqa: F401 -- re-exported for callers


def _route_after_verify(state: AgentState) -> str:
    if state.get("escalated"):
        return END
    if state.get("pending_approval"):
        # Paused on a human-in-the-loop interrupt (deep_agent.py's
        # INTERRUPT_ON) -- a stable resting state, same as escalated:
        # nothing left to do until an operator submits a decision via
        # POST /api/tasks/{id}/approve (which patches approval_decision and
        # re-invokes the graph, same resume mechanism as an escalation
        # resume -- see server.py's own comment on that endpoint).
        return END
    if state.get("pending_merge_approval"):
        # Parked for the operator's final look at the diff -- a resting state
        # exactly like pending_approval. POST /api/tasks/{id}/merge-decision
        # patches state and re-invokes the graph.
        return END
    # audit C-5: check pending_feedback BEFORE the approved-merge fast path.
    # If a fresh review came back NEEDS_FIXES for an approved sha, there is
    # feedback to act on and the approval is stale (the fix will be a new
    # commit) -- act on the feedback rather than re-attempting the same merge in
    # a loop. In the normal approved-merge flow there is no pending_feedback, so
    # this reordering doesn't affect it. (verify_and_ship also clears
    # merge_approved_sha on that NEEDS_FIXES return; this is belt-and-suspenders.)
    if state.get("pending_feedback"):
        return "work"
    if state.get("merge_approved_sha") and state.get("committed_sha"):
        # The operator approved the outstanding commit: run verify_and_ship
        # again so the SAME code path does the merge -- checks re-run, the
        # review service re-serves its cached READY verdict, and the approval
        # gate passes because the approved sha matches. Merging from the
        # endpoint instead would duplicate the episode/terminal bookkeeping
        # that only verify_and_ship knows how to do.
        return "verify_and_ship"
    # Not escalated, nothing to feed back -- the only path that returns in
    # this shape is a successful ship (verdict READY, merged and deployed).
    return END


def _work_retry_on(exc: Exception) -> bool:
    """Work node's retry predicate: default_retry_on plus TimeoutError.

    langchain-openai's StreamChunkTimeoutError (a mid-response stream stall)
    subclasses TimeoutError, which default_retry_on refuses (TimeoutError
    subclasses OSError, on its never-retry list). Work-node only,
    deliberately: verify_and_ship keeps the pure default specifically so
    wait_for_review's own already-waited-900s TimeoutError does not trigger
    a retry-from-scratch there.
    """
    if isinstance(exc, TimeoutError):
        return True
    return default_retry_on(exc)


def build_outer_graph(config: Config, checkpointer, store):
    # Bound via app_config=/pg_store= (not config=/store=) -- those literal
    # names are reserved by LangGraph's own node-kwarg auto-injection and
    # would silently override these partial-bound values.
    builder = StateGraph(AgentState)
    # Model failures no longer reach this policy: work_node retries them
    # itself, resuming the inner agent from its checkpoint, and hands the task
    # back escalated if the provider keeps failing (work.py,
    # TRANSIENT_MODEL_ERRORS) -- a failure escaping here after three quick
    # retries used to end the task as "error", work stranded. The policy stays
    # as the last net for anything raised outside the model stream.
    builder.add_node(
        "work",
        partial(work_node, app_config=config, checkpointer=checkpointer, pg_store=store),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=_work_retry_on),
    )
    # RetryPolicy(max_attempts=3): automatically retries the whole node
    # (with backoff+jitter) on an exception, using LangGraph's own default
    # retry_on predicate (retries connection errors and 5xx; does not retry
    # ValueError/TypeError/OSError/etc. -- notably TimeoutError is an
    # OSError subclass, so wait_for_review's own already-waited-900s
    # TimeoutError correctly does not trigger a retry-from-scratch here,
    # only genuinely fast/transient failures do). This is fast, automatic
    # recovery before ever reaching verify_and_ship_node's own try/except,
    # which is what makes a retry-from-scratch safe here at all -- see
    # verify_and_ship.py's own comments on committed_sha.
    builder.add_node(
        "verify_and_ship",
        partial(verify_and_ship_node, app_config=config, pg_store=store),
        retry_policy=RetryPolicy(max_attempts=3),
    )

    builder.add_edge(START, "work")
    builder.add_edge("work", "verify_and_ship")
    builder.add_conditional_edges(
        "verify_and_ship", _route_after_verify, {"work": "work", "verify_and_ship": "verify_and_ship", END: END}
    )

    return builder
