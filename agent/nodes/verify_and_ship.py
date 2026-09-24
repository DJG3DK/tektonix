"""The hard, code-enforced gate. The deep agent's own write_todos "completed"
status carries no authority here -- this node always re-runs the real
typecheck/lint/test suite itself, every pass, regardless of what the agent
believes or reports.

Four outcomes per pass:
  1. Checks fail -> inject the real failure output as pending_feedback,
     route back to "work" (same inner thread).
  2. Checks pass but there's no diff, for the first time this task -> nudge
     and loop back rather than silently doing nothing or shipping an empty
     diff.
  3. Checks pass but there's still no diff on a second consecutive pass ->
     terminal "done, no changes needed" outcome, not another nudge. Two
     consecutive no-diff passes is required so a task that hasn't started
     yet can't be mistaken for one that's genuinely finished.
  4. Checks pass and there's a real diff -> commit once for the whole task,
     hand off to the review service (review_gate.py); NEEDS_FIXES loops back
     to "work" with findings injected the same way; READY calls
     merge_and_deploy -> terminal.

The outer iteration/retry ceiling is checked first, before spending anything
on this round.

Also writes an episodic memory record at every terminal outcome (shipped or
escalated) -- a structured, queryable summary distinct from the semantic
/memories/AGENTS.md content. Not auto-loaded into any task's context; read
only by the consolidation agent (agent/consolidation.py), which distills
patterns across episodes into semantic memory updates on a schedule. Looping
(non-terminal) passes don't write an episode.

`_verify_and_ship`'s outer try/except converts any unexpected exception (a
transient review-service network blip, a subprocess spawn hiccup) into a
normal escalation instead of an uncaught exception reaching server.py as an
unresumable "error" status. `outer_graph.py`'s node also carries a
RetryPolicy for fast, automatic recovery from transient failures before this
needs an operator. Both are made safe by `committed_sha` (see
outer_state.py): once a commit succeeds, its sha is tracked in state so a
retry/resume with no new diff correctly resumes polling review for that
existing commit instead of stranding a real, unreviewed change.
"""

import os
import re
from contextlib import asynccontextmanager
import time

from langgraph.store.base import BaseStore

from agent.config import Config, PROJECTS
from agent.episodes import write_episode
from agent.tools.checks import run_all_checks
from agent.tools.git import (
    _git,
    current_sha,
    ensure_task_branch,
    git_commit,
    git_diff,
    rebase_onto_base,
)
from agent import check_timing
from agent import runtime_settings as _rs
from agent.tools.review_gate import (
    merge_and_deploy,
    ship_as_pull_request,
    trigger_check,
    wait_for_review,
)
from agent.project_checks import autodetect_checks_if_none
from agent.tools.git import sha_in_repo
from agent.outer_state import AgentState

# Duration lives in runtime_settings ("review_wait_timeout_s") so it can be
# raised without a restart when a large diff needs longer than the default.
# Minimum length for a final message to count as a genuine "no changes
# needed" conclusion rather than a truncated, mid-thought response.
MIN_CONCLUSION_CHARS = 120
# The END of a last response that announces work instead of concluding: "I have
# everything I need now ... Implementing now." ended a benchmark task as "done,
# no changes" on 2026-09-24 -- long enough to pass the length check, and an
# intention, not a conclusion. Matched on the final sentences only.
_ANNOUNCED_ACTION = re.compile(
    r"(?:\b(?:implementing|applying|making|writing|adding|fixing|proceeding|starting)\b[^.!?]{0,60}"
    r"\b(?:now|next|the (?:fix|change|edit)s?)\b"
    r"|\b(?:let me|i'll|i will|i am going to|i'm going to|next,? i|now i'll|now,? let me)\b)"
    r"(?:[^.!?]|\.(?=\S))*[.!?:]*\s*$",   # a dot inside a word (core.py) is not a sentence end
    re.IGNORECASE)
_NO_CHANGE_CONCLUSION = re.compile(
    r"\b(?:no (?:code )?changes? (?:is |are )?(?:needed|required|necessary)|already (?:fixed|handled|correct|works)|"
    r"nothing (?:needs|to) (?:be )?chang)", re.IGNORECASE)


def announces_unfinished_work(text: str) -> bool:
    """A final response whose last sentences say what it is ABOUT to do."""
    text = (text or "").strip()
    if not text or _NO_CHANGE_CONCLUSION.search(text):
        return False
    tail = " ".join(re.split(r"(?<=[.!?])\s+", text)[-2:])
    return bool(_ANNOUNCED_ACTION.search(tail))
# How many times in a row a terse final response may be nudged before the gate
# stops asking and lets the normal no-diff path decide. Without a cap this
# branch resets no_diff_streak every pass, so "no changes needed" can never be
# concluded -- a loop bounded only by max_iterations.
MAX_SHORT_CONCLUSION_NUDGES = 2
# Consecutive passes to nudge a model that's stopped acting on a rejected
# commit before escalating.
STALE_PENDING_REVIEW_LIMIT = 2
# Fresh-inner-thread restarts to try, after nudges are exhausted, before
# escalating -- a restart can recover from a degenerated conversation history
# that a nudge appended to the same history can't fix.
MAX_THREAD_RESTARTS = 1
# Consecutive passes to send the agent back to finish its own todo plan
# before committing anyway. This is a nudge budget, not a hard gate: the
# todo list is the model's own self-reported state, so a model that stops
# updating it (or writes a step it can't actually finish) must not be able
# to strand real, working, checks-passing code as an uncommittable diff
# forever. Past this, the work gets committed and reviewed as before.
INCOMPLETE_PLAN_LIMIT = 3


def _last_work_response_text(state: AgentState) -> str | None:
    """The most recent "work" node log entry's detail -- work.py populates
    this from the inner thread's actual final message content, so this
    reflects what the model said to end its turn.
    """
    for entry in reversed(state.get("execution_log", [])):
        if entry.get("node") == "work":
            detail = entry.get("detail")
            return detail if isinstance(detail, str) else None
    return None


def _escalate(reason: str) -> dict:
    return {
        "escalated": True,
        "escalation_reason": reason,
        "execution_log": [{
            "node": "verify_and_ship",
            "step_id": None,
            "summary": f"escalated: {reason}",
            "detail": "",
            "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    }


async def _apply_operator_edits(state: AgentState, repo_root: str, edits: dict) -> dict:
    """Write the operator's edited files onto the task branch's checkout.

    Refuses rather than guesses: the branch must still be at the sha the
    operator was editing, and every path must resolve inside the workspace,
    outside .git, and not through a symlink.
    """
    from agent.task_diff import valid_edit_path
    from agent.tools.git import task_branch_name

    branch = task_branch_name(state["task_id"])
    tip = await _git(f"rev-parse --verify --quiet refs/heads/{branch}", repo_root, timeout=15)
    if not tip["ok"] or tip["output"].strip() != edits.get("base_sha"):
        return {"ok": False, "reason": "the task's branch moved after the editor was opened -- reopen it"}

    cur = await _git("rev-parse --abbrev-ref HEAD", repo_root, timeout=15)
    status = await _git("status --porcelain", repo_root, timeout=15)
    if status["output"].strip():
        # Parked tasks leave a clean tree; anything here belongs to whoever
        # used the workspace since. Kept, not discarded.
        stash = await _git('stash push --include-untracked -m "tektonix: set aside for an operator edit"',
                           repo_root, timeout=120)
        if not stash["ok"]:
            return {"ok": False, "reason": f"workspace is dirty and could not be stashed: {stash['output'][:200]}"}
    if cur["output"].strip() != branch:
        co = await _git(f"checkout {branch}", repo_root, timeout=30)
        if not co["ok"]:
            return {"ok": False, "reason": f"could not check out {branch}: {co['output'][:200]}"}

    # Claimed, so that if the checks send this back to the agent, its next
    # pass reads the edit as this task's own uncommitted work and keeps it
    # instead of stashing it as another task's debris.
    from agent.tools.git import _claim_workspace
    await _claim_workspace(repo_root, state["task_id"])

    root = os.path.realpath(repo_root)
    for f in edits.get("files") or []:
        rel = valid_edit_path(f.get("path", ""))
        if rel is None:
            return {"ok": False, "reason": f"refused path {f.get('path')!r}"}
        full = os.path.join(root, rel)
        # Every existing component, not just the leaf: a symlinked directory
        # would carry the write out of the workspace just as well.
        probe = root
        for part in rel.split("/"):
            probe = os.path.join(probe, part)
            if os.path.islink(probe):
                return {"ok": False, "reason": f"refused {rel}: it goes through a symlink"}
        if os.path.commonpath([root, os.path.realpath(full)]) != root:
            return {"ok": False, "reason": f"refused {rel}: outside the workspace"}
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(f.get("content", ""))
    return {"ok": True}


def _unfinished_todos(state: AgentState) -> list[str]:
    """The agent's own remaining todo items, as it last reported them.

    `latest_todos` is written by work_node from the inner deep agent's
    write_todos state (TodoListMiddleware), so this is the model's own plan
    for its own task -- not an outer-graph notion of progress. Anything not
    "completed" counts as outstanding; a task with no todo list at all
    returns [] and is unaffected.
    """
    todos = state.get("latest_todos") or []
    return [
        str(t.get("content", ""))
        for t in todos
        if isinstance(t, dict) and t.get("status") != "completed"
    ]


def _loop_back(reason: str, feedback: str, state: AgentState, no_diff_streak: int = 0) -> dict:
    return {
        "iteration_count": state["iteration_count"] + 1,
        "pending_feedback": feedback,
        "no_diff_streak": no_diff_streak,
        "execution_log": [{
            "node": "verify_and_ship",
            "step_id": None,
            "summary": reason,
            "detail": feedback[:2000],
            "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    }


def _done_no_changes(state: AgentState) -> dict:
    """Terminal, non-escalated completion for a task that genuinely needed
    no code changes -- distinct from `_escalate` (nothing went wrong) and
    from a real ship (nothing was committed/reviewed/deployed). Routes to
    END the same way a successful ship does (no escalated, no
    pending_feedback) -- see outer_graph.py's _route_after_verify.
    """
    return {
        "no_diff_streak": 0,
        "execution_log": [{
            "node": "verify_and_ship",
            "step_id": None,
            "summary": "done -- no changes needed, confirmed on two consecutive passes",
            "detail": "",
            "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    }


async def _write_episode(store: BaseStore, state: AgentState, result: dict,
                         config: Config) -> None:
    """Work out how this task ended, and hand the record to the one writer.

    The inference is here because it is about THIS node's four outcomes; the
    writing is in agent/episodes.py because several other things are about
    to want a say in it (see that module).

    `config` has no default on purpose. It is unused downstream today and a
    default would have let the three existing callers stay untouched -- and
    then the one wire this refactor exists to lay would be the one thing
    nothing asserts, until an embedding or an index hook needed it and found
    it None.
    """
    if result.get("escalated"):
        outcome = "escalated"
    elif result.get("review_gate_result") is None:
        # Terminal with no review_gate_result means nothing was ever committed
        # for review -- the only path that reaches here is _done_no_changes.
        outcome = "done_no_changes"
    else:
        outcome = "shipped"
    record = {
        "task_id": state["task_id"],
        "goal": state["goal"],
        "outcome": outcome,
        "escalation_reason": result.get("escalation_reason"),
        "review_verdict": (result.get("review_gate_result") or {}).get("verdict"),
        "cost_usd": state["cost_so_far"],
        "iteration_count": state["iteration_count"],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    await write_episode(store, config, state["repo"], record)


def _is_terminal(result: dict) -> bool:
    if result.get("escalated"):
        # First, so that an escalation is never buried behind a parking flag
        # left in the same result. A task that gave up has a real outcome to
        # record; the two guards below are about work that is still in play.
        # (No current path sets both -- verify_and_ship returns early on each
        # -- so this is the function saying what it means rather than a fix.)
        return True
    if result.get("pending_approval"):
        # Paused waiting on an operator decision, not a real outcome yet --
        # must not write an episode even though this shares the same
        # "no pending_feedback" shape a real terminal outcome has.
        return False
    if result.get("pending_merge_approval"):
        # The SAME reasoning, and it was missing here until 2026-09-22.
        #
        # A READY verdict parked on the operator's final look has not shipped:
        # the work is on a branch, live is untouched, and the operator may
        # still send it back. Writing an episode here recorded outcome
        # "shipped" for a commit that had merged nowhere.
        #
        # That is not a cosmetic mislabel. agent/consolidation.py learns from
        # episodes, and a "shipped" one is read as work that LANDED -- so on
        # 2026-09-21 a parked task taught this project's memory that "a root
        # package.json now exists". The operator never merged. For a day the
        # memory asserted a file that was not on main, a planning session
        # believed it, concluded there was nothing to build, and a build task
        # started against a goal that was an essay explaining as much.
        #
        # The episode is not lost, only deferred: approving re-enters this
        # node, the merge happens, and the terminal write runs then -- with
        # the outcome it actually had.
        return False
    return not result.get("pending_feedback")


async def verify_and_ship_node(state: AgentState, app_config: Config, pg_store: BaseStore) -> dict:
    # app_config/pg_store (not config/store) -- see outer_graph.py's own
    # comment on why those literal names collide with LangGraph's node-kwarg
    # auto-injection.
    result = await _verify_and_ship(state, app_config, pg_store)
    if _is_terminal(result):
        await _write_episode(pg_store, state, result, app_config)
    return result


async def _verify_and_ship(state: AgentState, config: Config, store: BaseStore | None = None) -> dict:
    if state.get("escalated"):
        # work_node already escalated this pass -- the graph's own
        # work->verify_and_ship edge is unconditional, so without this guard
        # a real check run would execute on a pass whose outcome is
        # discarded anyway. Re-assert escalated/escalation_reason explicitly
        # so _is_terminal/_write_episode see the escalation on this node's
        # own return value.
        return {
            "escalated": True,
            "escalation_reason": state.get("escalation_reason"),
        }

    if state.get("pending_approval"):
        # work_node is paused mid-turn on a human-in-the-loop interrupt
        # (deep_agent.py's INTERRUPT_ON) -- nothing to verify or ship yet.
        # Same reasoning as the escalated guard above: running checks
        # against a task that's mid-approval-wait would be discarded
        # regardless (_route_after_verify routes to END on pending_approval
        # the same way it does on escalated).
        return {"pending_approval": state["pending_approval"]}

    if state["iteration_count"] >= state["max_iterations"]:
        return _escalate(f"hit max_iterations ({state['max_iterations']}) without completing")

    repo = state["repo"]
    # One task at a time per project from here to the end of the node: the
    # checks, the commit, the review and the merge. Tasks code in parallel
    # (parallel_tasks_per_project), but the reviewer reviews one branch of a
    # project at a time and a merge moves the base every other task rebases
    # onto, so this is where they take turns. Uncontended -- a no-op -- with
    # the default of one task per project.
    async with _ship_gate(repo, config):
        return await _verify_and_ship_gated(state, config, store, repo)


@asynccontextmanager
async def _ship_gate(repo: str, config: Config | None):
    from agent.graph import project_slot

    async def _announce():
        try:
            from langgraph.config import get_stream_writer
            get_stream_writer()({"type": "log_entry", "entry": {
                "node": "verify_and_ship", "step_id": None,
                "summary": "waiting for another task on this project to finish its review and merge",
                "detail": "", "cost_usd": 0.0,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }})
        except Exception:  # noqa: BLE001 -- outside a graph run there is no stream
            pass

    async with project_slot(repo, getattr(config, "dsn", None), slots=1, scope="ship",
                            on_wait=_announce):
        yield


async def _verify_and_ship_gated(state: AgentState, config: Config | None, store: BaseStore | None,
                                 repo: str) -> dict:
    # The task's own workspace, made if it is missing: a task can reach this
    # node straight from a resume, with no work pass before it, and its
    # workspace may have been cleaned up since -- or never exist, for a task
    # from before workspaces were per-task. ensure() is instant when it is there.
    from agent import workspaces
    try:
        repo_root = (await workspaces.ensure(repo, state["task_id"]))["path"]
    except Exception as e:  # noqa: BLE001
        esc = _escalate(f"could not prepare this task's workspace: {e}")
        if state.get("committed_sha"):
            esc["committed_sha"] = state["committed_sha"]
        return esc

    if state.get("committed_sha") and not state.get("operator_edits"):
        # A task with a commit ships from its own branch, whoever used the
        # workspace last. Every path below -- the approved fast path, the
        # rebase on a moved base, the no-diff re-review -- reads or moves
        # the CHECKOUT, and a heal or a resume reaches this node without a
        # work pass to put it there first. (A hand edit does its own, with a
        # stricter check -- see _apply_operator_edits.)
        from agent.tools.git import restore_task_workspace
        restored = await restore_task_workspace(repo_root, state["task_id"], rebase=False)
        if not restored.get("ok"):
            return _escalate(f"could not put the workspace on this task's branch: {restored.get('reason')}")

    if state.get("operator_edits"):
        # Applied here and nowhere else: this node runs inside the task's own
        # graph run, which holds the project lock. From here it is ordinary
        # uncommitted work -- committed, checked and reviewed like the
        # agent's, and parked again for the operator's final look.
        edits = state["operator_edits"]
        try:
            applied = await _apply_operator_edits(state, repo_root, edits)
        except Exception as e:  # noqa: BLE001
            applied = {"ok": False, "reason": str(e)}
        if not applied["ok"]:
            return {**_escalate(f"operator edit not applied: {applied['reason']}"), "operator_edits": None}
        state = {**state, "operator_edits": None, "_operator_edit": edits}
        try:
            out = await _verify_and_ship_inner(state, repo, repo_root, store)
        except Exception as e:  # noqa: BLE001 -- same conversion as below
            out = _escalate(f"verify_and_ship failed: {e}")
        return {**out, "operator_edits": None}

    try:
        return await _verify_and_ship_inner(state, repo, repo_root, store)
    except Exception as e:  # noqa: BLE001 -- converts any transient failure
        # (a review-service network blip, a subprocess spawn hiccup in
        # run_all_checks) into a normal, resumable escalation instead of an
        # uncaught exception reaching server.py as an unresumable "error"
        # status.
        #
        # audit C-6: PRESERVE committed_sha across the escalation. _escalate
        # alone returns only escalated/reason/log, so a commit made just before
        # the exception (e.g. trigger_check raising right after the commit) was
        # dropped -- on resume git_diff is empty, pending_sha is None, and the
        # node concludes "no changes needed" while a real, unreviewed commit
        # sits on the task branch reporting success. Carrying committed_sha
        # forward is exactly what makes a resume re-poll review for it instead.
        esc = _escalate(f"verify_and_ship failed: {e}")
        sha = state.get("committed_sha")
        if sha:
            esc["committed_sha"] = sha
        return esc


async def _verify_and_ship_inner(state: AgentState, repo: str, repo_root: str,
                                 store: BaseStore | None = None) -> dict:
    # ── Fast path: the operator just approved the outstanding commit ────────
    # On the post-approval re-entry, nothing about the code has changed since
    # this gate last ran: checks already passed for this exact sha, the review
    # service issued READY for it, and the sha equality in the approval gate
    # guarantees the approval is for this commit and no other. Re-running the
    # full check suite here re-verified an unchanged commit and turned every
    # "Approve & merge" click into 6-8 minutes of silence on a large project (51
    # suites) before the merge actually happened. Jump straight to the
    # review/merge sequence -- wait_for_review re-serves its cached verdict
    # and the merge proceeds in seconds.
    approved = state.get("merge_approved_sha")
    if approved and approved == state.get("committed_sha") and not state.get("pending_feedback"):
        # Only when there is nothing newer in the tree. A work pass after the
        # approval means new work, and shipping the approved commit past it
        # is how a resumed task on 2026-09-23 tried to land its OLD commit
        # and died on "cannot rebase: You have unstaged changes".
        dirty = await _git("status --porcelain", repo_root, timeout=15)
        if dirty["ok"] and not dirty["output"].strip():
            return {**await _review_and_deploy(state, repo, approved), "incomplete_plan_streak": 0}

    # Announce the check phase BEFORE it runs. The suite takes 6-8 minutes on
    # a large project and emits nothing while it grinds, which reads in the dashboard as
    # a task frozen mid-step -- reported as a stall twice in one night. One
    # stream event turns dead air into an explained wait.
    # `phase` and `expected_seconds` are for the dashboard's idle banner, which
    # otherwise answers this silence with "the agent is either on a long model
    # call or stuck" -- directly beneath this very line saying the quiet is
    # normal. Two contradicting sentences on one screen, and the operator
    # reasonably believed the alarming one (2026-09-13). The estimate is this
    # PROJECT's own median: webapp's suite is 111 tests and runs ~310s, a small
    # repo's takes twenty, and no single threshold is right for both.
    expected = await check_timing.expected_seconds(store, repo)
    try:
        from langgraph.config import get_stream_writer
        get_stream_writer()({"type": "log_entry", "entry": {
            "node": "verify_and_ship", "step_id": None,
            "summary": "running the full check suite (typecheck/lint/tests) — several minutes of quiet is normal here",
            "detail": "", "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": "checks",
            "expected_seconds": expected,
        }})
    except Exception:  # noqa: BLE001 -- a missing stream context must never block the checks themselves
        pass

    _checks_started = time.monotonic()
    checks = await run_all_checks(repo_root, repo)
    # Measured whether they passed or failed: a failing suite still took that
    # long, and the banner's question is "is this silence normal", not "did it
    # work".
    await check_timing.record(store, repo, time.monotonic() - _checks_started)
    if not checks["all_ok"]:
        feedback = (
            "The deterministic check suite FAILED -- read this carefully, it's the specific reason, "
            "not a vague \"try again\":\n\n" + checks["summary"] + "\n\n"
            "Do not just re-assert that it works. Actually fix the specific failure above, verify it "
            "yourself with run_checks before reporting done -- this gate will re-run the same checks "
            "regardless, so if you don't verify first you're just guessing whether this attempt "
            "actually fixed it."
        )
        return _loop_back("checks failed, looping back with feedback", feedback, state, no_diff_streak=0)

    diff = await git_diff(repo_root)
    pending_sha = state.get("committed_sha")
    if not diff.strip():
        # Self-commit absorption: if the model ran `git commit` itself
        # instead of leaving the commit to this gate, HEAD may have moved
        # past the tracked pending sha. A self-committed HEAD is still real,
        # unreviewed-by-us work -- adopt it as the pending commit and
        # proceed normally rather than polling for a stale sha that will
        # never get a verdict.
        if pending_sha:
            head_sha = await current_sha(repo_root)
            if head_sha and head_sha != pending_sha:
                pending_sha = head_sha
        if pending_sha:
            # Nothing new since a commit that's still pending review/deploy.
            # Two distinct cases:
            #   1. Resuming after an escalation or a transient failure
            #      interrupted the review/deploy sequence below -- no
            #      verdict recorded yet for this exact sha. Go straight to
            #      (re-)triggering review for it, same as always.
            #   2. We already have a non-READY verdict for this exact sha,
            #      and the model has stopped acting on it. Re-triggering the
            #      review service's check suite on an unchanged commit is
            #      deterministic and would just get the identical verdict
            #      back forever, burning compute for zero chance of a
            #      different answer. Detecting "we already know the answer
            #      for this sha" and nudging (then escalating if the nudge
            #      doesn't work either) replaces that waste with either real
            #      progress or a timely human handoff.
            #
            # If the review service auto-merges and deploys on READY, it may
            # also consume its own state entry for that verdict -- so by the
            # time this gate looks, live already contains the commit and no
            # verdict exists to find. Checking live directly avoids nudging
            # an already-finished task in circles: if live already has this
            # sha, the work is shipped, so conclude instead of re-reviewing.
            live_root = (PROJECTS.get(repo) or {}).get("live")
            if live_root and await sha_in_repo(live_root, pending_sha):
                shipped_log = [{
                    "node": "verify_and_ship",
                    "step_id": state["task_id"],
                    "summary": "already merged and deployed (auto-merge on READY) -- concluding",
                    "detail": f"live repo at {live_root} already contains pending commit {pending_sha}",
                    "cost_usd": 0.0,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }]
                # This is a ship too -- the merge just happened on the review
                # service's side -- so the first-merge check detection must
                # run here as well, or a project whose every merge lands via
                # auto-merge would never get its checks.
                checks_entry = await autodetect_checks_if_none(repo)
                if checks_entry is not None:
                    shipped_log.append(checks_entry)
                return {
                    "committed_sha": None, "pending_merge_approval": None, "merge_approved_sha": None,  # shipped -- nothing left to track
                    "review_gate_result": {"verdict": "READY", "lastReviewedSha": pending_sha},
                    "stale_pending_review_streak": 0,
                    "execution_log": shipped_log,
                }

            prior_review = state.get("review_gate_result") or {}
            if prior_review.get("lastReviewedSha") == pending_sha and prior_review.get("verdict") != "READY":
                streak = state.get("stale_pending_review_streak", 0) + 1
                if streak > STALE_PENDING_REVIEW_LIMIT:
                    # Nudges exhausted. If the inner conversation history has
                    # degenerated (e.g. summarization compressed away every
                    # tool-calling exchange, leaving only text-only
                    # responses), nothing appended to that history can fix
                    # it. Restart the inner thread fresh (bump
                    # inner_thread_generation -- work.py derives a new
                    # thread_id from it), seeded via pending_feedback with
                    # the distilled context a fresh start needs. Nothing real
                    # is lost -- the work itself is safe in the pending
                    # commit, not the conversation. Only one restart per task
                    # (MAX_THREAD_RESTARTS): if a clean thread stalls the
                    # same way, the problem isn't conversation shape and a
                    # human should look.
                    generation = state.get("inner_thread_generation", 0)
                    if generation >= MAX_THREAD_RESTARTS:
                        return _escalate(
                            f"no progress for {streak} consecutive passes after the review service rejected "
                            f"{pending_sha[:12]}, even after a fresh-thread restart -- stopped acting on "
                            f"the feedback (describing a plan without ever calling a tool) instead of "
                            f"fixing it. Needs a human look."
                        )
                    findings = "\n".join(
                        f"- [{f.get('severity', '?')}] {f.get('file', '')}: {f.get('issue', '')}"
                        for f in prior_review.get("findings", [])
                    )
                    prior_detail = (prior_review.get("agentMessage")
                                    or f"{prior_review.get('summary', '')}\n\n{findings}")
                    fresh_context = (
                        f"You are continuing an in-progress task. THE GOAL:\n{state['goal']}\n\n"
                        f"Work so far is committed as {pending_sha[:12]} (see `git log`/`git show` for "
                        f"what it contains) -- do NOT start over, build on it. The review service rejected "
                        f"that commit with these findings, which are the current blockers:\n\n"
                        f"{prior_detail}\n\n"
                        f"Fix these by ACTUALLY EDITING FILES with your edit/write tools, starting now. "
                        f"Do not restate this plan back -- your first action should be a tool call."
                    )
                    return {
                        **_loop_back(
                            "nudges exhausted on a stale pending review -- restarting the inner thread fresh",
                            fresh_context, state, no_diff_streak=0,
                        ),
                        "inner_thread_generation": generation + 1,
                        "stale_pending_review_streak": 0,  # fresh thread, fresh chances
                        "committed_sha": pending_sha,
                    }
                feedback = (
                    f"You have NOT changed any files since the review service rejected this commit "
                    f"({pending_sha[:12]}) -- re-triggering review on an unchanged commit would just "
                    f"get the identical verdict again, so this gate won't do that. The findings from "
                    f"that rejection are still the real, current blockers:\n\n"
                    f"{prior_review.get('agentMessage') or prior_review.get('summary', '')}\n\n"
                    f"Actually make the file changes now -- call the edit/write tools. Describing a "
                    f"plan in text again will not be treated as progress."
                )
                return {
                    **_loop_back(
                        f"pending review unchanged for {streak} consecutive passes -- nudging instead of re-polling",
                        feedback, state, no_diff_streak=0,
                    ),
                    "stale_pending_review_streak": streak,
                    "committed_sha": pending_sha,  # still outstanding, explicit per this file's own convention
                }
            return await _review_and_deploy(state, repo, pending_sha)

        # The two-consecutive-no-diff "done" rule assumes a no-diff pass's
        # final message is a genuine decision, which isn't always true: a
        # model can end a pass having announced an intended next action
        # ("let me look at X") without a tool call following through on it,
        # and without a token-limit truncation to explain the drop. A short
        # final message is a cheap signal for this -- a genuine "no changes
        # needed" explanation runs several sentences; a dropped intention
        # does not. Treating a short pass as NOT incrementing the streak
        # costs at most one extra pass on a false positive (a real
        # conclusion that happened to be terse), versus silently ending the
        # task on a dropped tool call.
        # A SHORT final response, not a missing one. The distinction is the
        # whole fix of 2026-09-13: work.py used to read this back from the
        # inner agent's checkpoint, where `messages` does not exist, so it was
        # "" on every pass -- and "" is shorter than any threshold, so this
        # branch fired every time, reset the streak, and made the
        # two-consecutive-no-diff exit unreachable. Task 25e2bfb0 looped on it
        # twice in eight minutes, each time told to follow through on an
        # intention it had never announced. work.py takes the text from the
        # stream now; an empty value here means genuinely unknown, and guessing
        # "cut off mid-thought" from nothing is what caused the loop.
        last_response = (_last_work_response_text(state) or "").strip()
        nudges = state.get("short_conclusion_streak", 0)
        looks_incomplete = bool(last_response) and (
            len(last_response) < MIN_CONCLUSION_CHARS or announces_unfinished_work(last_response))
        if looks_incomplete and nudges < MAX_SHORT_CONCLUSION_NUDGES:
            feedback = (
                "Your last response looks like it was cut off mid-thought -- it announced what you "
                "were about to do (e.g. \"let me look at X\") but ended without a tool call actually "
                "doing it, and without a real conclusion either. This does NOT count as a decision "
                "that nothing needs fixing. Either follow through with the action you described, or "
                "if you've genuinely finished investigating, write your full conclusion and reasoning "
                "explicitly -- not a one-line intention."
            )
            return {
                **_loop_back(
                    "no diff -- last response looked cut off mid-thought, not a real conclusion",
                    feedback, state, no_diff_streak=0,
                ),
                "short_conclusion_streak": nudges + 1,
            }
        # Asked twice and still terse: take it at face value rather than
        # spending another whole pass on the same nudge. The streak is NOT
        # reset here, so the ordinary no-diff path below decides the outcome --
        # which is the bound this branch was missing.

        # A benchmark task's statement is to change the source: an empty
        # patch always fails it, so "done, no changes" is never its ending.
        # Bounded like any other loop-back, by the budget and max_iterations.
        if (PROJECTS.get(state.get("repo") or "") or {}).get("benchmark"):
            return _loop_back(
                "benchmark task with no diff -- it requires a source change",
                "There are still no file changes, and this task requires a change to the library's "
                "source code: an empty change cannot resolve it. Make the fix now with `edit`, then "
                "verify it.", state, no_diff_streak=1)
        if state.get("no_diff_streak", 0) >= 1:
            return _done_no_changes(state)
        feedback = (
            "Checks pass, but there are no file changes yet (git diff is empty). If you're still "
            "investigating, continue. If you believe the goal is already satisfied with no changes "
            "needed, say so explicitly and explain why, rather than stopping silently -- this will be "
            "checked once more, and if it's still true next pass, the task will end here as a "
            "legitimate no-changes-needed completion."
        )
        return _loop_back("checks passed but no diff -- nudging for progress", feedback, state, no_diff_streak=1)

    # Don't ship a half-finished plan. The agent writes itself a todo list
    # up front, and it used to be free to reach this gate with most of that
    # list still pending -- checks pass on the part it HAS done, so the diff
    # got committed and sent for review mid-plan. The review service then
    # reviews an intentionally-incomplete commit as if it were the finished
    # article, and correctly reports the not-yet-written pieces (the tests,
    # the migration script) as defects. That bounces the task on findings
    # that describe scheduled work rather than actual problems, and burns a
    # full review round doing it (confirmed live 2026-08-23: a task
    # committed at step 5 of its own 8-step plan and was rejected for a
    # missing backfill script that was step 6, plus missing tests that were
    # step 7). Sending it back to finish first is strictly cheaper than
    # reviewing, bouncing, and re-reviewing.
    #
    # Nudge budget, not a hard gate -- see INCOMPLETE_PLAN_LIMIT for why a
    # model that stops maintaining its own list must not be able to strand
    # working code uncommitted forever.
    unfinished = _unfinished_todos(state)
    plan_streak = state.get("incomplete_plan_streak", 0)
    if unfinished and plan_streak < INCOMPLETE_PLAN_LIMIT and not state.get("_operator_edit"):
        remaining = "\n".join(f"- {item}" for item in unfinished[:12])
        feedback = (
            "Checks pass and you have real changes, but your own plan still has unfinished "
            f"items, so this has NOT been committed or sent for review yet:\n\n{remaining}\n\n"
            "Finish them now. Anything already done needs marking completed in your todo list "
            "-- that list is what this gate reads, so an item left un-ticked reads as "
            "outstanding no matter how much work you actually did. If an item genuinely "
            "should not be done (it turned out unnecessary, or it's out of scope), say so "
            "explicitly and mark it completed rather than leaving it hanging. Your work so "
            "far is safe in the working tree; nothing has been lost."
        )
        return {
            **_loop_back(
                f"plan not finished ({len(unfinished)} item(s) left) -- holding the commit",
                feedback, state, no_diff_streak=0,
            ),
            "incomplete_plan_streak": plan_streak + 1,
        }

    # A real uncommitted diff -- commit it. If a prior commit was still
    # pending review (pending_sha set), this naturally folds any new work on
    # top of it into one fresh combined commit, which supersedes the old sha
    # (committed_sha gets overwritten below, in _review_and_deploy).
    goal = state["goal"]
    commit_message = f"{goal}\n\n(shipped via deepagents-based agent)"
    operator_edit = state.get("_operator_edit")
    if operator_edit:
        note = (operator_edit.get("note") or "").strip()
        commit_message += (f"\n\nIncludes a hand edit by {operator_edit.get('by') or 'the operator'}"
                           + (f": {note}" if note else "") + ".")

    # Commit onto a per-task branch, never the sandbox's `main`. `main` stays a
    # pure mirror the refresh cron can fast-forward, and the reviewer gets a
    # branch + fixed merge-base as its review unit instead of inferring one by
    # comparing two HEADs. See ensure_task_branch for the failure this fixes.
    branch = await ensure_task_branch(repo_root, state["task_id"])
    if not branch["ok"]:
        return _escalate(
            f"could not switch to task branch {branch['branch']}: "
            f"{branch.get('output', '')[:500]}"
        )

    commit = await git_commit(repo_root, commit_message)
    if not commit["ok"]:
        return _escalate(f"final commit failed: {commit['output'][:500]}")

    # Live may have moved while this task was working. Rebasing here, rather
    # than only when the merge refuses, has a second benefit: the review then
    # measures this branch against the base it will actually merge into. A
    # review against a stale base can miss a conflict entirely.
    #
    # Cheap when nothing moved, which is the usual case: two rev-parses.
    rebase = await rebase_onto_base(repo_root)
    if rebase.get("conflicts"):
        return {
            "iteration_count": state["iteration_count"] + 1,
            "pending_feedback": (
                "Live moved on while you were working, and rebasing onto it conflicts in:\n\n"
                + "\n".join(f"  - {f}" for f in rebase["conflicts"]) + "\n\n"
                "Re-apply your change on top of what is there now. Read those files first -- "
                "somebody else edited them after you started."
            ),
            "no_diff_streak": 0,
            "committed_sha": await current_sha(repo_root),
        }
    if rebase.get("rebased"):
        print(f"[verify] {repo}: live moved during the task; rebased onto {rebase['base'][:12]}")

    sha = await current_sha(repo_root)
    # The plan read complete on this pass (or the nudge budget ran out), so
    # start that budget over -- a later loop-back can legitimately add fresh
    # todo items, and those deserve their own full set of nudges rather than
    # inheriting a streak from earlier in the task.
    return {**await _review_and_deploy(state, repo, sha), "incomplete_plan_streak": 0}


async def _review_and_deploy(state: AgentState, repo: str, sha: str) -> dict:
    """Triggers (or re-triggers, on a resume) a review-service pass for
    `sha` and acts on the verdict. `committed_sha` is set to `sha` on every
    non-terminal-success return path here (loop-back, escalation) so a
    resume always knows which commit is still outstanding; cleared only
    once merge_and_deploy actually succeeds.
    """
    # audit C-6: trigger_check is an unguarded POST to the review control port.
    # If it raised (service mid-restart, connection refused, a 502), the
    # exception escaped to the catch-all WITHOUT committed_sha having been
    # written yet -- the freshly-made commit was then lost and the task falsely
    # concluded "no changes needed". Set committed_sha on the way in, and give
    # trigger_check the same single-retry transient tolerance wait_for_review
    # already has, so a blip escalates recoverably (committed_sha carried)
    # instead of stranding a real commit.
    # The branch this commit is on, so the reviewer reviews THIS work rather
    # than whichever of the project's parked branches it would guess at.
    from agent.tools.git import task_branch_name
    branch = task_branch_name(state["task_id"])
    try:
        await trigger_check(repo, branch)
    except Exception:  # noqa: BLE001
        import asyncio as _asyncio
        await _asyncio.sleep(3)
        try:
            await trigger_check(repo, branch)
        except Exception as e2:  # noqa: BLE001
            return {"committed_sha": sha, **_escalate(f"could not trigger review for {sha[:12]}: {e2}")}
    # Same reasoning as the check announcement, and this one was missing
    # entirely: the review service can be quiet for the whole of
    # review_wait_timeout_s (15 minutes by default) with nothing on screen.
    try:
        from langgraph.config import get_stream_writer
        get_stream_writer()({"type": "log_entry", "entry": {
            "node": "verify_and_ship", "step_id": None,
            "summary": f"waiting for the review service on {sha[:12]} — it runs its own checks, so this is quiet for minutes",
            "detail": "", "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": "review",
            "expected_seconds": None,
        }})
    except Exception:  # noqa: BLE001
        pass

    try:
        review = await wait_for_review(repo, sha, timeout=_rs.as_int("review_wait_timeout_s"), branch=branch)
    except TimeoutError as e:
        return {"committed_sha": sha, **_escalate(str(e))}

    log_entry = {
        "node": "verify_and_ship",
        "step_id": None,
        "summary": f"review service verdict: {review['verdict']}",
        "detail": review.get("summary", ""),
        "cost_usd": 0.0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if review["verdict"] != "READY":
        findings = "\n".join(f"- [{f['severity']}] {f.get('file', '')}: {f['issue']}" for f in review.get("findings", []))
        # The gate builds the wording now, and it is the only version carrying
        # the failure OUTPUT. Without it a rejection said which checks failed
        # but never why, so the only way to find out was to re-run them by
        # hand -- in a workspace provisioned differently from the gate's, which
        # is how a run can spend rounds fixing something that was never broken.
        detail = review.get("agentMessage") or f"{review.get('summary', '')}\n\n{findings}"
        # audit M-14: honor the reviewer's own circuit breaker. It sets
        # `escalated` after MAX_CONSECUTIVE_FIXES non-converging rounds or when a
        # single file churns repeatedly (built for an observed 8-round loop), and
        # its comments say it will "stop nudging and tell the agent to halt for a
        # human instead." That flag was previously read nowhere here, so the
        # detector only moved a dashboard badge. Now it actually halts: loop back
        # only while NOT escalated; once escalated, hand off to a human.
        if review.get("escalated"):
            reason = (
                f"The independent review service escalated this after repeated non-converging "
                f"rounds (its churn/consecutive-NEEDS_FIXES circuit breaker fired) -- a human "
                f"should look rather than the agent nudging again.\n\n{detail}"
            )
            return {
                "committed_sha": sha,
                "review_gate_result": review,
                "execution_log": [log_entry],
                "stale_pending_review_streak": 0,
                **_escalate(reason),
            }
        feedback = (
            f"The review service (an independent adversarial check, separate from the checks above) "
            f"found real issues and rejected this:\n\n{detail}\n\n"
            f"Fix these specifically, then let this gate re-review."
        )
        return {
            "iteration_count": state["iteration_count"] + 1,
            "pending_feedback": feedback,
            "no_diff_streak": 0,
            "committed_sha": sha,
            "review_gate_result": review,
            "execution_log": [log_entry],
            # audit C-5: clear the merge approval on this non-shipping return.
            # The approved-merge fast path re-triggers a FRESH review of the same
            # sha, and a model reviewer can legitimately return NEEDS_FIXES on a
            # second pass (or because live main moved). Leaving merge_approved_sha
            # set sent the router back into verify_and_ship every lap -- never
            # reaching work, spending a trigger_check + up to 900s wait_for_review
            # each time, until the iteration ceiling escalated. The agent's fix
            # will be a NEW commit needing its own fresh approval anyway.
            "merge_approved_sha": None,
            # A real review just ran for this sha -- whatever streak was
            # counting "stuck re-polling the same stale verdict" no longer
            # applies to this fresh verdict.
            "stale_pending_review_streak": 0,
        }

    # ── Operator's final look ────────────────────────────────────────────
    # The review service is a MODEL's opinion; this pause is the operator's.
    # When the task was created by a user with require_merge_review on (the
    # default), a READY verdict parks the task instead of merging, the UI
    # shows the full diff, and the operator either approves (merge-decision
    # endpoint patches merge_approved_sha and re-runs this node) or sends it
    # back with notes (patches pending_feedback -> work). The sha equality
    # check is what makes approval safe against races: an approval can only
    # ever ship the exact commit the operator was shown, never one that
    # landed after they looked.
    if state.get("require_merge_review") and state.get("merge_approved_sha") != sha:
        pause_entry = {
            "node": "verify_and_ship",
            "step_id": None,
            "summary": "review READY — waiting for operator's final look before merge",
            "detail": f"sha {sha[:12]} approved by the review service; merge is parked on your decision.",
            "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return {
            "committed_sha": sha,
            "review_gate_result": review,
            "pending_merge_approval": {
                "sha": sha,
                "repo": repo,
                "review_summary": review.get("summary", ""),
                "at": time.time(),
            },
            "execution_log": [log_entry, pause_entry],
            "stale_pending_review_streak": 0,
        }

    # How this project ships. "push" fast-forwards the base branch and deploys,
    # which is what every existing project does and stays the default. "pr"
    # opens a pull request instead and touches the base branch not at all --
    # for a repository whose base branch the agent is not allowed to write.
    #
    # The gate is identical either way: this is only what happens after a pass.
    ship_mode = (PROJECTS.get(repo) or {}).get("ship", "push")
    if ship_mode == "pr":
        from agent.tools.git import task_branch_name
        branch = task_branch_name(state["task_id"])
        deployed = await ship_as_pull_request(repo, branch, sha, state["goal"].splitlines()[0][:72])
    else:
        deployed = await merge_and_deploy(repo, branch)
    # Say what actually happened. "merged and deployed" after a pull request
    # opened is simply untrue -- nothing merged, nothing deployed, and the one
    # thing the operator needs is the link, which was buried in a stringified
    # dict.
    if not deployed["ok"] and deployed.get("reason") == "diverged":
        # Not a failure: the base moved, and the next few lines rebase onto it
        # and review again. Calling it FAILED sent an operator looking for a
        # problem while the system was busy fixing one.
        ship_summary = "the base moved on — rebasing onto it and reviewing again"
    elif not deployed["ok"]:
        ship_summary = "merge/deploy FAILED" if ship_mode != "pr" else "could not open a pull request"
    elif deployed.get("shipped") == "pull_request":
        ship_summary = f"pull request opened: {deployed.get('pull_request', '')}"
    else:
        ship_summary = "merged and deployed"
    deploy_entry = {
        "node": "verify_and_ship",
        "step_id": None,
        "summary": ship_summary,
        "detail": str(deployed)[:2000],
        "cost_usd": 0.0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if not deployed["ok"] and deployed.get("reason") == "diverged":
        # Live moved between the review and the ship. On the merge path
        # --ff-only then makes landing impossible, and that rule is the whole
        # guarantee that what merges is what was reviewed -- so the branch
        # moves instead: rebase onto the new tip and go round the review once
        # more. Before this, a reviewed and approved commit simply had nowhere
        # to land and the task stopped, having already been paid for.
        #
        # The pull-request path reaches here too, since 2026-09-22. It never
        # fast-forwards, so it never reported anything wrong -- it just pushed
        # the branch exactly as cut and opened a PR that was already behind,
        # and where the changes overlapped, one that would not merge at all.
        # Same base moving, same answer.
        from agent.workspaces import workspace_for
        repo_root = workspace_for(repo, state["task_id"])
        rb = await rebase_onto_base(repo_root)
        if rb.get("conflicts"):
            # Genuine disagreement between two changes. The agent has the task
            # context and is still running, so it gets the file list -- the
            # same shape as a review finding. The rebase was aborted, so the
            # branch is exactly where it was.
            feedback = (
                "Live moved on while this was being reviewed, and rebasing onto it conflicts "
                "in:\n\n" + "\n".join(f"  - {f}" for f in rb["conflicts"]) + "\n\n"
                "Re-apply your change on top of what is there now. Read the current content of "
                "those files first -- somebody else edited them after you started."
            )
            return {
                "iteration_count": state["iteration_count"] + 1,
                "pending_feedback": feedback,
                "no_diff_streak": 0,
                "committed_sha": sha,
                "merge_approved_sha": None,
                "review_gate_result": review,
                "execution_log": [log_entry, deploy_entry],
                "stale_pending_review_streak": 0,
            }
        if not rb.get("rebased"):
            return {
                "committed_sha": sha,
                "merge_approved_sha": None,
                "review_gate_result": review,
                "execution_log": [log_entry, deploy_entry],
                **_escalate(
                    "live moved on and the branch could not be rebased onto it: "
                    f"{rb.get('output') or rb.get('reason') or 'unknown'}"
                ),
            }
        # Rebased cleanly. The sha changed, so the verdict that approved the
        # old one no longer applies -- the review service checks exactly that,
        # and it is right to. One more round, on the same work.
        new_sha = await current_sha(repo_root)
        same = " The patch is unchanged; this is the same work on a newer base." \
            if rb.get("patch_identical") else ""
        print(f"[verify] {repo}: live moved, rebased {sha[:12]} -> {new_sha[:12]}.{same}")
        return {
            **await _review_and_deploy(state, repo, new_sha),
            "execution_log": [log_entry, deploy_entry],
        }

    if not deployed["ok"]:
        # "build" failures are real compile/typecheck errors in the code the
        # agent just wrote -- the same kind of thing checks/NEEDS_FIXES loop
        # back for, and equally fixable by the agent, so loop back instead
        # of escalating. "merge" (git conflict from a concurrent commit) and
        # "restart" (infra failure) are not code problems the agent can fix,
        # so those still escalate. Either way, a resume/retry safely re-runs
        # the whole review+merge+deploy sequence rather than double-merging,
        # since the review service's own merge endpoint re-gates on current
        # review state server-side.
        if deployed.get("stage") == "build":
            feedback = (
                f"The deploy build failed after the review service approved this commit -- a real "
                f"compile/typecheck error, not a review finding:\n\n{deployed.get('error', '')}\n\n"
                f"Fix the actual code error above, then let this gate re-review and re-deploy."
            )
            return {
                "iteration_count": state["iteration_count"] + 1,
                "pending_feedback": feedback,
                "no_diff_streak": 0,
                "committed_sha": sha,
                # The approval was CONSUMED by this merge attempt. Leaving it
                # set wedged a live task (2026-08-26): approved==committed
                # routed back into verify instead of letting pending_feedback
                # reach work, and the loop-back never ran. The agent's fix
                # will be a NEW commit needing its own fresh approval anyway.
                "merge_approved_sha": None,
                "review_gate_result": review,
                "execution_log": [log_entry, deploy_entry],
                "stale_pending_review_streak": 0,
            }
        # The MESSAGE, not the repr of the dict carrying it. Escalation text is
        # the last thing an operator reads before deciding what to do, and
        # until 2026-09-22 this printed the whole result object:
        #
        #   merge/deploy failed: {'ok': False, 'stage': 'ship', 'error': 'could
        #   not push agent/7991...: To https://github.com/...\n ! [remote
        #   rejected] ...'}
        #
        # -- a stringified dict with the actual sentence buried inside it and
        # its newlines escaped. review_gate now writes a real explanation into
        # `error` for the cases it can recognise, and that has to survive the
        # trip out rather than being re-wrapped in punctuation.
        why = str(deployed.get("error") or "").strip() or str(deployed)
        stage = deployed.get("stage") or "merge/deploy"
        return {
            "committed_sha": sha,
            "merge_approved_sha": None,  # same reasoning as the build branch above
            "review_gate_result": review,
            "execution_log": [log_entry, deploy_entry],
            **_escalate(f"{stage} failed: {why}"),
        }

    # The merge is live, so this is the first moment a brand-new project has
    # real code for detection to look at. Only when the REVIEWER says it runs
    # no checks (its built-ins can carry checks projects.json cannot see);
    # never on a failed merge/deploy above. Never raises -- an exception here
    # would turn a shipped task into an escalation via the outer try/except.
    shipped_log = [log_entry, deploy_entry]
    checks_entry = await autodetect_checks_if_none(repo)
    if checks_entry is not None:
        shipped_log.append(checks_entry)
    out = {
        "committed_sha": None,  # shipped -- nothing left to track
        "pending_merge_approval": None,
        "merge_approved_sha": None,  # consumed by this merge; a future commit needs its own approval
        "review_gate_result": review,
        "execution_log": shipped_log,
    }
    # A pull request is not finished work, it is work waiting for a person.
    # Carried on the task itself so the dashboard can link it rather than
    # making somebody read the step log to find out where it went.
    if deployed.get("shipped") == "pull_request":
        out["pull_request_url"] = deployed.get("pull_request")
    return out
