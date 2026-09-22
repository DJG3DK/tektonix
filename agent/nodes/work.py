"""work_node -- manually drives a deepagents-built agent (see agent/deep_agent.py)
for one outer-graph pass. Not native LangGraph subgraph nesting: the outer
AgentState and the deep agent's own state share no keys, so
`builder.add_node("work", deep_agent_graph)` would produce a schema-merge
mess for zero benefit. This is a plain node function that invokes the deep
agent's own `.astream()` against a derived, distinct thread_id
(f"{task_id}:work") on the same Postgres checkpointer/store instances the
outer graph uses -- one pool, two thread namespaces that never collide.

Four cases for what to send the deep agent this pass, decided from outer
state alone (no inspection of the inner thread's own state needed):
  0. Resuming after a human-in-the-loop approval decision (approval_decision
     set -- see deep_agent.py's INTERRUPT_ON): takes priority over every
     other case. Sends `Command(resume={"decisions": [...]})`, not a
     HumanMessage -- LangGraph's native interrupt-resume protocol, which
     resumes the same inner thread exactly where interrupt() paused it, with
     the model seeing whatever ToolMessage the decision produces (approve:
     the tool actually runs; reject: a synthetic rejection message).
  1. First-ever invocation for this task (iteration_count == 0, no pending
     feedback): send the goal as the initial HumanMessage.
  2. Looping back after verify_and_ship rejected the prior attempt
     (pending_feedback set): inject that as a new HumanMessage into the same
     inner thread -- the deep agent keeps its own todo-list/message
     continuity rather than starting over.
  3. Resuming after an orphaned restart (iteration_count > 0, no pending
     feedback -- verify_and_ship never got to inject anything because the
     process died mid-"work"): send no new input at all, just continue the
     existing inner thread from its own checkpoint.

Streaming: uses `agent.astream_events(..., version="v3")`, not plain
`.astream(stream_mode="updates")` -- subagent-delegated work (e.g. an
investigator or test-writer subagent) is invisible to `stream_mode="updates"`
on the coordinator's own thread, since deepagents runs subagents via a
`task()` tool call whose internal steps never surface as top-level graph
updates. `run.values` (a full state snapshot per superstep, not a delta)
drives the root coordinator's own progress -- new messages are detected by
tracking how many messages have been seen so far, since `values` reports the
whole accumulated list every time. `run.subagents` yields a typed handle the
moment each subagent starts, exposing its own `.values` the same way the root
run does, so subagent progress is consumed by the identical `_consume`
helper, just tagged with the subagent's name instead of "work". All consumers
run concurrently via asyncio.gather -- required because subagent handles can
arrive (and need consuming) while the root run is still mid-stream.

Every consumer shares ONE already-seen set, and that is load-bearing. Each
projection reports its whole accumulated message list on every superstep, so
a consumer that starts with an empty set republishes the entire history it can
see. A per-consumer set therefore replays everything each time a handle
arrives: measured on task 279c29fd (2026-09-12), 1328 of 1864 logged tool
results were replays, the same burst reappearing verbatim up to three times
(`{bash: 74, write: 3, write_todos: 2}` at 17:59, 18:38 and 18:46). That is
what "the same commands are being spammed" in the live view was -- a harness
artefact, not a model loop -- and it inflated every count on the Analytics
tool panel with it. Message ids are unique, so one shared set is both
sufficient and necessary.

Human-in-the-loop: after the values/subagents consumption above completes
(which happens naturally when the graph pauses at an interrupt() -- no
exception, the stream just ends there), `run.interrupted()`/`run.
interrupts()` are checked to distinguish "the agent actually finished this
turn" from "a tool call matched deep_agent.py's INTERRUPT_ON policy and the
graph is paused waiting for operator approval". A real
`langgraph.types.Interrupt` object's `.value` is exactly the HITLRequest
dict HumanInTheLoopMiddleware built -- stored as-is in `pending_approval` for
the dashboard to render and an operator to act on.
"""

import asyncio
import logging
import time

import openai
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.types import Command

from agent.config import Config
from agent.deep_agent import build_deep_agent
from agent.message_text import content_text
from agent import plan_progress
from agent import tool_events
from agent.tools import bash_advice
from agent.messages import pop_messages
from agent.middleware.budget_guard import BudgetExceededError
from agent.model_config import resolve_alias
from agent.outer_state import AgentState
from agent.tools.router_ledger import RouterLedger

logger = logging.getLogger("tektonix")


def inner_thread_config(task_id: str, repo: str, generation: int = 0) -> dict:
    # metadata/tags -- standard RunnableConfig fields LangChain's tracer
    # picks up automatically, so a task's inner (deep-agent) trace is
    # filterable the same way its outer-graph trace is.
    #
    # `generation` > 0 selects a fresh inner thread for the same task -- see
    # outer_state.py's inner_thread_generation. This exists because a
    # degenerated conversation history (e.g. summarization compressing away
    # every tool-calling exchange) can leave a model pattern-matching a
    # text-only response shape indefinitely; no message appended to that
    # history can fix it, so a fresh thread (seeded with distilled context
    # via pending_feedback -- the actual work stays safe in its git commit)
    # is the escape hatch. Generation 0 keeps the historical un-suffixed id
    # so existing tasks resume their current thread unchanged.
    suffix = f":work:g{generation}" if generation else ":work"
    return {
        "configurable": {"thread_id": f"{task_id}{suffix}"},
        "metadata": {"task_id": task_id, "repo": repo},
        "tags": [repo],
    }


def _translate_message(task_id: str, node_label: str, msg) -> dict | None:
    """One LangChain message -> one LogEntry-shaped custom event, for the
    dashboard's live view. Message-type-based rather than node-name-based --
    this is what identifies a real model turn vs. a tool result, regardless
    of which internal node (coordinator or a named subagent) produced it.
    """
    if isinstance(msg, AIMessage):
        if msg.tool_calls:
            calls = ", ".join(f"{tc['name']}({str(tc.get('args'))[:80]})" for tc in msg.tool_calls)
            summary = f"calling: {calls}"
        else:
            # content_text, never str(content): block-list content (Kimi via
            # OpenRouter) rendered as raw dict repr, which the operator read
            # as failed/no-output calls -- see agent/message_text.py.
            text = content_text(msg.content).strip()
            if not text:
                return None
            summary = text[:200]
        # A pinned role's response echoes back the bare alias
        # (return_raw_model_name only applies to the shared auto_router
        # deployment, not these entries), so resolve_alias() is the only
        # way to badge each turn with the real model that actually answered
        # -- read fresh from config.yaml every time, deliberately, so this
        # reflects whatever an operator has pinned right now, not whatever
        # it was when this process started. See resolve_alias's own
        # docstring for the incident that made staleness here a real bug,
        # not a hypothetical one.
        response_metadata = getattr(msg, "response_metadata", None) or {}
        alias = response_metadata.get("model_name") or response_metadata.get("model")
        model = resolve_alias(alias)
        return {
            "node": node_label,
            "step_id": task_id,
            "summary": summary,
            "detail": (content_text(msg.content).strip() or summary)[:2000],
            "cost_usd": 0.0,  # real cost is tracked/reported by BudgetGuardMiddleware, not duplicated per-event here
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": model,
            # The alias BEFORE resolve_alias is the role itself (agent-coder,
            # agent-test-writer, ...). Carried separately because two roles can
            # legitimately pin the SAME underlying model -- coder and
            # test-writer both on deepseek made the model badge alone
            # ambiguous: an error in the stream could not be attributed to a
            # role without this (operator request, 2026-08-26).
            "role": alias.removeprefix("agent-") if isinstance(alias, str) else None,
        }

    if isinstance(msg, ToolMessage):
        text = content_text(msg.content)
        # One line per result, so the reliability panel is ours rather than
        # LangSmith's (agent/tool_events.py). Name and outcome only -- the
        # output is the part that carries secrets, and it is already in the
        # stream this function feeds.
        tool_events.record(
            tool=getattr(msg, "name", None) or "unknown",
            ok=getattr(msg, "status", "success") != "error",
            task_id=task_id,
            # Set when the harness prepended a nudge to this result (a shell
            # command that was really a file edit, a read, or a reach into the
            # agent's own memory). A field on the call, not a tool of its own.
            nudge=bash_advice.kind_of_result(text),
            detail=text[:200] if getattr(msg, "status", "success") == "error" else None,
        )
        return {
            "node": node_label,
            "step_id": task_id,
            "summary": f"tool result: {text[:200]}",
            "detail": text[:2000],
            "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    return None


async def _consume_values(task_id: str, node_label: str, proj, writer, seen: dict, tracker=None,
                          seen_ids: set | None = None, final_text: dict | None = None) -> None:
    """Drains one `run.values`-shaped projection (root or a subagent handle),
    translating newly-seen messages/todos into custom events as they land.
    `seen` is a per-projection dict (`{"todos": Any, "cost": float}`) so
    the same accumulated state isn't re-emitted every superstep.

    `seen_ids` is the message-id set, and it is shared across ALL consumers of
    one run -- see this module's docstring on the replay it prevents. Defaults
    to a set inside `seen` so a single-projection caller (a test) still works.

    `final_text` collects the COORDINATOR's last assistant text as it streams.
    The pass's final message used to be read back from the inner agent's
    checkpoint afterwards, and that channel is not there: measured on
    2026-09-13, `aget_state` returns todos but no `messages`, so the final
    summary was the empty string on every pass. That is load-bearing --
    verify_and_ship's no-diff gate calls a final response shorter than 120
    chars "cut off mid-thought", loops back, and resets no_diff_streak, so an
    always-empty value meant a task with no diff could never reach the
    two-consecutive-passes "no changes needed" exit. Task 25e2bfb0 looped on
    exactly that, twice in eight minutes, being told to follow through on an
    intention it had never announced. Taking the text from the stream uses
    what we demonstrably have.

    `tracker` (the shared BudgetTracker) makes live cost visible mid-pass:
    cost_so_far otherwise only crosses to the dashboard on outer node
    boundaries, and one work pass can run well past ten minutes. Emitted
    only when the number actually moved, so it adds no chatter to an idle
    stream.
    """
    async for values in proj:
        if not isinstance(values, dict):
            continue
        todos = values.get("todos")
        if node_label == "work" and todos is not None and todos != seen.get("todos"):
            seen["todos"] = todos
            writer({"type": "todos", "todos": todos})
        messages = values.get("messages") or []
        # By identity, not by count: after SummarizationMiddleware compacts
        # the thread the list is SHORTER than the count already published,
        # and a count-based check goes silent until the thread regrows past
        # it -- the "no movement for 20 minutes" reports of 2026-09-09 on a
        # coder that was calling every 40 seconds (same fix in planning_chat).
        ids = seen_ids if seen_ids is not None else seen.setdefault("msg_ids", set())
        for msg in messages:
            key = str(getattr(msg, "id", None) or f"obj:{id(msg)}")
            if key in ids:
                continue
            ids.add(key)
            translated = _translate_message(task_id, node_label, msg)
            if translated:
                writer({"type": "log_entry", "entry": translated})
            # The coordinator's own prose only: a subagent's closing remark is
            # not this pass's conclusion, and the gate reads it as one.
            if final_text is not None and node_label == "work" and isinstance(msg, AIMessage):
                text = content_text(msg.content).strip()
                if text:
                    final_text["text"] = text[:2000]
        if tracker is not None:
            cost = tracker.total_cost
            # Any real move emits (operator ask 2026-08-28: "update on every
            # call that has a cost"). The old $0.005 step swallowed cheap
            # calls -- a run of small cached-context calls could go many
            # steps with a frozen display. The superstep cadence already
            # bounds event volume; a sub-tenth-of-a-cent epsilon only guards
            # float noise.
            if cost - seen.get("cost", 0.0) >= 0.0001:
                seen["cost"] = cost
                writer({"type": "cost", "cost_so_far": cost})


def _reconciled_cost(state: AgentState) -> float:
    """What this task has really spent, including what a killed pass spent.

    `cost_so_far` lives in the outer checkpoint, which is written when a pass
    RETURNS. A pass that dies -- pm2's memory watchdog, a restart, a crash --
    takes its spend with it: on 2026-09-12 a task displayed $0.76 after
    spending about $9, because four passes had been killed and each resume
    started again from the last completed boundary. The money was real and the
    ceiling was being enforced against a number that had rolled back.

    The router's ledger knows. Every call carries this task's id now, so the
    log beside the proxy can be totalled for the task and compared with what
    the checkpoint believes. The larger of the two wins: the ledger cannot
    undercount (a call it has not written yet is simply absent, and the
    estimate covers it), and the checkpoint cannot overcount.
    """
    checkpointed = float(state.get("cost_so_far") or 0.0)
    task_id = state.get("task_id")
    if not task_id:
        return checkpointed
    try:
        billed = RouterLedger().total_for_task(task_id)
    except Exception:  # noqa: BLE001 -- a ledger read must never stop a pass
        logger.exception("ledger total failed for %s; using the checkpoint", task_id)
        return checkpointed
    if billed > checkpointed + 0.005:
        logger.info("cost reconciled for %s: checkpoint $%.2f -> router ledger $%.2f",
                    task_id, checkpointed, billed)
        return billed
    return checkpointed


async def work_node(state: AgentState, app_config: Config, checkpointer, pg_store) -> dict:
    # Parameter names `app_config`/`pg_store` are deliberate, not stylistic:
    # LangGraph auto-injects its own runtime object into any node parameter
    # literally named `config` (when unannotated or annotated
    # RunnableConfig) or `store` (when unannotated or annotated BaseStore),
    # silently overriding whatever `functools.partial(work_node, config=...,
    # store=...)` bound -- see langgraph/_internal/_runnable.py's
    # KWARGS_CONFIG_KEYS.
    task_id = state["task_id"]
    repo = state["repo"]

    # Fresh task, first pass: move the workspace to the current live tip
    # BEFORE the agent reads or edits anything. Safe here and only here -- the
    # whole graph run holds project_lock (server.py), iteration_count==0 with
    # no committed_sha means no prior pass owns the tree, and the sync itself
    # refuses to act on a dirty tree. Without this, tasks branched from
    # wherever the last task left HEAD: stale bases, stale code being edited,
    # and --ff-only merges failing on "diverging branches" (observed live
    # 2026-08-26, a branch 9 commits behind main).
    if state["iteration_count"] == 0 and not state.get("committed_sha"):
        from agent.tools.git import fetch_base_from_origin, sync_workspace_to_base
        from agent.config import PROJECTS

        # The local checkout is a cache of GitHub, not the source of truth.
        # Fetch FIRST, then move the workspace, so "the current live tip" means
        # the current one rather than whatever this machine last saw. Against
        # the live repo: the workspace is a worktree of it and shares its refs.
        #
        # Every failure here is ordinary -- no remote, offline, a host that is
        # down, or local commits that cannot fast-forward -- and none of them
        # stops the task. It runs from the local tip exactly as it used to.
        live_root = (PROJECTS[repo] or {}).get("live")
        if live_root:
            fetched = await fetch_base_from_origin(live_root)
            if fetched.get("advanced"):
                print(f"[work] {repo}: origin had moved; live main now at {fetched['base'][:12]}")
            elif fetched.get("diverged"):
                print(f"[work] {repo}: local main has commits origin does not -- left alone")

        # task_id, so the sync can tell this task's own uncommitted work
        # (a resume) from debris a different task abandoned. Without it every
        # dirty tree looks the same and the safe answer is to refuse, which
        # is how a task ends up silently running on a stale base.
        sync = await sync_workspace_to_base(PROJECTS[repo]["sandbox"], task_id=task_id)
        print(f"[work] {repo}: workspace sync -> {sync}")
        if sync.get("salvaged_from"):
            # Said out loud. Something was stashed to make room for this task,
            # and an operator who wants it back needs to know it exists.
            print(f"[work] {repo}: stashed work left behind by {sync['salvaged_from']} "
                  f"-- recover it with `git stash list` in the workspace")
        elif not sync.get("synced"):
            # A skipped sync used to return ok=True and vanish into a log
            # line. It is the failure this function exists to prevent, so it
            # says so.
            print(f"[work] {repo}: WORKSPACE NOT SYNCED ({sync.get('reason')}) "
                  f"-- this task starts from whatever HEAD was already at")

    agent, tracker, last_failed_edit_ref = await build_deep_agent(
        app_config,
        repo,
        budget_usd=state["budget_usd"],
        checkpointer=checkpointer,
        store=pg_store,
        starting_cost=_reconciled_cost(state),
        # Stamped onto every model call this pass makes, so the router's own
        # ledger can total the task (see _reconciled_cost).
        task_id=task_id,
        # The edit-repeat guard (agent_tools.py) is scoped to this
        # build_deep_agent call's own closure, which is fresh every pass --
        # round-tripping its final state through AgentState (the same
        # mechanism used for committed_sha/no_diff_streak) is what makes it
        # catch a repeat that spans multiple separate work<->verify_and_ship
        # loop-backs, not just repeats within one uninterrupted pass.
        starting_last_failed_edit=state.get("last_failed_edit_signature"),
        # Captured at task creation (see outer_state.py) -- a task runs under
        # the gate its creator had at the time, not whatever is set now.
        auto_approve_commands=state.get("auto_approve_commands", False),
        # So the prompts can name the files this task is meant to CREATE --
        # their absence is the expected starting state, not a blocker
        # (agent/new_files.py).
        goal=state.get("goal") or "",
        # Frontend work runs on the Kimi coder seat (agent/frontend_route.py);
        # decided at creation, carried in state so a resume keeps it.
        route=state.get("route", "general"),
        # The other projects this task may READ, for "use the one in X as a
        # template". Captured at creation from its creator's access; absent
        # on a task checkpointed before this existed, which then reads
        # nothing but its own repo (agent/tools/reference_tools.py).
        reference_repos=state.get("reference_repos") or [],
    )
    inner_config = inner_thread_config(task_id, repo, state.get("inner_thread_generation", 0))
    pending_feedback = state.get("pending_feedback")
    approval_decision = state.get("approval_decision")

    if approval_decision:
        # Case 0 -- see module docstring. Takes priority over everything
        # else: an approval decision resumes the same paused turn, it
        # doesn't make sense alongside a fresh goal/feedback message.
        graph_input = Command(resume={"decisions": approval_decision})
    else:
        messages: list = []
        if pending_feedback:
            messages.append(HumanMessage(content=pending_feedback))
        elif state["iteration_count"] == 0:
            messages.append(HumanMessage(content=state["goal"]))
        # Operator messages (server.py's /message endpoint, agent/messages.py's
        # mailbox) drain here, at the start of a work pass -- not mid-stream.
        # This is a known limitation: there's no mechanism to inject a
        # message into an actively-streaming inner astream_events() run
        # (that would need its own interrupt()/Command(resume=...) design --
        # the HITL approval gate below uses exactly that mechanism, but only
        # for a model-initiated pause, not an operator-initiated one). A
        # message sent while "work" is deep in a long unsupervised run sits
        # in the mailbox until the next work pass starts, which can be much
        # later since one "work" pass can span many turns before
        # verify_and_ship loops back. Still drained and delivered eventually
        # rather than silently discarded.
        for text in pop_messages(task_id):
            messages.append(HumanMessage(content=f"[operator message] {text}"))

        graph_input = {"messages": messages} if messages else None  # None: orphan-restart resume, case 3

    escalated = False
    escalation_reason = None
    final_summary = ""
    # The coordinator's closing prose, collected as it streams. Declared out
    # here, not inside the stream block, so the enrichment below can always
    # read it -- same reasoning as subagent_tasks.
    final_text: dict = {}
    latest_todos: list | None = None
    pending_approval: dict | None = None

    # Surfaces live progress through the outer graph's own custom-event
    # channel -- server.py's _stream_graph republishes these to the
    # dashboard WS as they happen, not batched up and delivered only when
    # this whole node returns. get_stream_writer() requires a runnable
    # context in the installed langgraph version -- it raises RuntimeError
    # if work_node is invoked directly instead of through the compiled outer
    # graph's own .astream()/.ainvoke(). Always test this node by driving it
    # through build_outer_graph(), never by calling work_node() bare.
    writer = get_stream_writer()

    # Declared outside the try so `finally` can always reach it -- a subagent
    # consumer is spawned via asyncio.ensure_future (fire-and-forget, not
    # awaited inline), so if the root consumption raises anywhere (e.g. a
    # BudgetExceededError surfacing mid-stream) the try block jumps straight
    # to `except`, skipping the in-try join and leaving already-spawned
    # subagent tasks running detached. `finally` guarantees every task gets
    # cancelled and drained on every exit path.
    subagent_tasks: list[asyncio.Task] = []
    try:
        async with await agent.astream_events(graph_input, config=inner_config, version="v3") as run:
            root_seen: dict = {}
            # One set for the whole run, every consumer. A per-consumer set
            # replayed each projection's entire history every time a subagent
            # handle arrived -- see this module's docstring.
            seen_ids: set = set()

            async def _consume_subagents() -> None:
                async for handle in run.subagents:
                    label = f"work:{handle.name or 'subagent'}"
                    subagent_tasks.append(
                        asyncio.ensure_future(_consume_values(
                            task_id, label, handle.values, writer, {}, tracker, seen_ids=seen_ids,
                            final_text=final_text))
                    )

            await asyncio.gather(
                _consume_values(task_id, "work", run.values, writer, root_seen, tracker,
                                seen_ids=seen_ids, final_text=final_text),
                _consume_subagents(),
            )
            if subagent_tasks:
                await asyncio.gather(*subagent_tasks)

            if root_seen.get("todos") is not None:
                latest_todos = root_seen["todos"]

            # The values/subagents consumption above ends normally (no
            # exception) both when the agent genuinely finished its turn and
            # when it hit a HumanInTheLoopMiddleware interrupt() -- the
            # stream just has nothing more to yield until a decision resumes
            # it. run.interrupted()/interrupts() are what distinguish the
            # two. See deep_agent.py's INTERRUPT_ON.
            if await run.interrupted():
                interrupts = await run.interrupts()
                if interrupts:
                    pending_approval = interrupts[0].value
    except BudgetExceededError as e:
        escalated = True
        escalation_reason = f"cost budget exhausted (${e.spent:.2f} >= ${e.budget:.2f})"
    except TimeoutError:
        # langchain-openai's StreamChunkTimeoutError (stream stalled --
        # chunks stopped arriving mid-response) subclasses TimeoutError, not
        # openai.APIError, so the branch below wouldn't catch it. Treated the
        # same as APIError: a transient transport pathology, which is exactly
        # what RetryPolicy exists for. outer_graph.py's retry_on accepts
        # TimeoutError explicitly, since langgraph's default_retry_on refuses
        # it (TimeoutError subclasses OSError, which is on its no-retry list).
        raise
    except openai.APIError:
        # A malformed (non-JSON) tool-call-arguments generation from an
        # underlying model can surface here as an APIError from the
        # provider. Escalating the whole task on the very first occurrence
        # would be excessive for what's very likely a one-off flaky
        # generation from one model, since the router picks adaptively per
        # call and a fresh retry has a real chance of landing on a
        # different, non-flaky model entirely. Re-raising (instead of
        # swallowing into an escalation) lets outer_graph.py's RetryPolicy on
        # "work" retry the whole node automatically, fast, before ever
        # bothering a human. Deliberately narrower than "any Exception":
        # this only covers openai/API-layer failures (bad requests, rate
        # limits, connection errors, 5xxs), not our own code's bugs.
        raise
    except Exception as e:  # noqa: BLE001 -- any failure here must still surface as an escalation, not crash the outer graph
        escalated = True
        escalation_reason = f"work node failed: {e}"
    finally:
        for t in subagent_tasks:
            if not t.done():
                t.cancel()
        if subagent_tasks:
            await asyncio.gather(*subagent_tasks, return_exceptions=True)

    # tracker.total_cost is authoritative regardless of how the loop above
    # ended (normal completion, budget trip, or any other exception) -- it's
    # been accumulating from real per-call cost the whole time.
    cost_so_far = tracker.total_cost

    # Secondary backstop, defense-in-depth against the primary
    # (BudgetGuardMiddleware) somehow not tripping (e.g. a future subagent
    # added without the middleware explicitly attached). Should essentially
    # never fire in practice.
    if not escalated and cost_so_far >= state["budget_usd"]:
        escalated = True
        escalation_reason = f"cost budget exhausted (${cost_so_far:.2f} >= ${state['budget_usd']:.2f}) [outer backstop]"

    # Always fetched (not just on success) -- latest_todos should reflect
    # whatever the deep agent's own thread actually has, even on an
    # escalated pass, so the dashboard's plan view doesn't regress to blank.
    try:
        final_state = await agent.aget_state(inner_config)
        if latest_todos is None:
            latest_todos = final_state.values.get("todos")
        # write_todos replaces the list, and a model asked to update its plan
        # often writes only what is LEFT -- so the completed half has to be
        # carried forward here, not just in the dashboard. The commit gate
        # reads this value: an all-pending rewrite of a finished plan would
        # otherwise hold the task open (see agent/plan_progress.py).
        latest_todos = plan_progress.merge_todos(state.get("latest_todos"), latest_todos)
        if not escalated:
            messages = final_state.values.get("messages", [])
            if messages:
                final_summary = content_text(messages[-1].content)[:2000]
    except Exception:  # noqa: BLE001 -- best-effort enrichment, never worth failing the whole node over
        pass

    # The stream is the reliable source; the checkpoint read above is kept as a
    # fallback for the case where a pass produced no streamed prose at all.
    # Measured 2026-09-13: `messages` is absent from the inner agent's
    # reconstructed state, so that read yields "" every time -- and an empty
    # value here is what verify_and_ship's no-diff gate misreads as "cut off
    # mid-thought", forever.
    if not final_summary.strip():
        final_summary = final_text.get("text", "")

    if pending_approval:
        summary = f"awaiting approval: {pending_approval['action_requests'][0]['name']}"
    elif escalated:
        summary = f"escalated: {escalation_reason}"
    else:
        summary = "work pass complete"

    log_entry = {
        "node": "work",
        "step_id": None,
        "summary": summary,
        "detail": final_summary,
        "cost_usd": cost_so_far - state["cost_so_far"],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    return {
        "cost_so_far": cost_so_far,
        "escalated": escalated,
        "escalation_reason": escalation_reason,
        "pending_feedback": None,  # consumed
        "approval_decision": None,  # consumed (only ever set on a case-0 resume pass)
        "pending_approval": pending_approval,
        "latest_todos": latest_todos,
        "execution_log": [log_entry],
        # Whatever the edit-repeat guard's state was at the end of this pass
        # (a JSON string, or None if the last edit succeeded/none were
        # attempted) -- fed back into build_deep_agent's own tools on the
        # next pass so the guard survives a verify_and_ship loop-back
        # instead of resetting to empty every time. See agent_tools.py.
        "last_failed_edit_signature": last_failed_edit_ref.get("signature"),
    }
