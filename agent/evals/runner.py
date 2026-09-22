"""Run the golden tasks, for real, and write down what happened.

This drives the SAME compiled graph a production task runs -- work node,
check suite, commit, the real reviewer. Not `_stream_graph`: that is server
plumbing (websocket fan-out, task meta, transcript recorders) and none of it
changes what the agent does. What is under test is the graph.

THE ONE THING IT DOES DIFFERENTLY IS NOT MERGING, AND IT DOES NOT NEED A
SPECIAL MODE TO DO IT. `require_merge_review=True` already parks a task after
a READY verdict, waiting for the operator's final look; the harness is simply
an operator who never clicks approve. The verdict is recorded, the episode is
written, `_route_after_verify` returns END, and no branch is merged anywhere.
A second code path for "run but do not ship" would be a path production never
takes, and it would drift.

WHAT MAKES A RUN COMPARABLE TO THE NEXT ONE.

Every task starts from a rebuilt fixture, so the repository state is identical
each time. The classifier is skipped -- the spec declares the category -- so a
one-shot model call cannot reroute a task between runs and change what is
being compared. Everything else, including which coder seat the task lands on,
is decided exactly as production decides it.

AND IT STOPS ON MONEY. Each task is a real agent run, so the suite tracks
cumulative spend and halts the moment the ceiling is crossed, reporting what
it has rather than continuing quietly.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agent.evals import fixtures as fx
from agent.evals.assertions import AssertionContext, TaskAssertionReport, evaluate
from agent.evals.spec import TaskSpec

logger = logging.getLogger("tektonix.evals")

# A task that has not finished in this long is not going to. The ceiling is
# generous because a real task legitimately spends minutes in the check suite
# and up to fifteen more waiting on a review -- but it has to exist, or one
# wedged task costs the whole run.
TASK_TIMEOUT_S = 45 * 60


@dataclass
class TaskRun:
    """One golden task, start to finish."""
    task: TaskSpec
    task_id: str
    outcome: str = "unknown"      # shipped | escalated | done_no_changes
                                  # | blocked | error | skipped
    escalation_reason: str | None = None
    review_verdict: str | None = None
    checks_pass: bool | None = None
    iteration_count: int | None = None
    cost_usd: float = 0.0
    duration_s: float = 0.0
    changed_paths: tuple[str, ...] = ()
    assertions: TaskAssertionReport = field(default_factory=TaskAssertionReport)
    error: str = ""

    @property
    def passed(self) -> bool:
        """A golden task passes only on its assertions.

        Deliberately not `outcome == "shipped"`. A task can ship, pass its
        checks and earn a READY verdict having "fixed" the bug by weakening
        the test -- which is the failure this suite exists to catch, and the
        one every gate in the system is blind to.
        """
        return (self.outcome not in ("error", "skipped", "blocked")
                and self.assertions.passed)


@dataclass
class SuiteRun:
    started_at: float
    window_label: str
    runs: list[TaskRun] = field(default_factory=list)
    stopped_early: str = ""
    total_cost: float = 0.0

    @property
    def passed(self) -> int:
        return sum(1 for r in self.runs if r.passed)

    @property
    def attempted(self) -> list[TaskRun]:
        return [r for r in self.runs if r.outcome != "skipped"]


def _derive_route(task: TaskSpec) -> tuple[str, str]:
    """Which coder seat, decided the way production decides it.

    Not hardcoded per fixture: the routing decision is part of what a change
    to this agent can break, so the eval has to exercise the real one. A
    ui-styling task landing on the general seat is a regression the suite
    should notice, and it cannot notice what it has pinned.
    """
    from agent.frontend_route import classify_frontend
    decision = classify_frontend(task.goal, task.category, None)
    return decision.route, decision.reason


async def _final_state(graph, thread_config) -> dict:
    snapshot = await graph.aget_state(thread_config)
    return dict(snapshot.values) if snapshot and snapshot.values else {}


def _read_outcome(state: dict) -> tuple[str, str | None]:
    """The same inference verify_and_ship makes when it writes an episode,
    plus the two resting states that are not outcomes at all.

    Kept in step with that function on purpose: an eval classifying outcomes
    its own way would disagree with the Analytics panel it is meant to be
    comparable to. But `_is_terminal` ALSO returns False for
    `pending_approval`, so no episode is written for a parked task -- and the
    first end-to-end run is what made that matter. Every task stopped on a
    shell-command approval, and this function reported them as
    "done_no_changes": a task that had not started read as a task that had
    finished and correctly decided there was nothing to do. Two tasks, a
    green-looking terminal outcome, and $0.004 spent.
    """
    if state.get("escalated"):
        return "escalated", state.get("escalation_reason")
    if state.get("pending_approval"):
        requests = (state["pending_approval"] or {}).get("action_requests") or [{}]
        return "blocked", f"parked awaiting approval for: {requests[0].get('name', 'a tool call')}"
    if state.get("pending_merge_approval"):
        # The resting state this harness AIMS for: a READY verdict, parked on
        # the operator's final look, nothing merged. review_gate_result is set,
        # so it falls through to "shipped" below -- said here so the reader
        # does not have to reconstruct why it is absent.
        pass
    if state.get("review_gate_result") is None:
        return "done_no_changes", None
    return "shipped", None


async def run_task(task: TaskSpec, *, graph, config, eval_root: Path,
                   run_command=None) -> TaskRun:
    """One task: rebuild its fixture, run the graph, score the result."""
    from agent.evals.spec import load_fixture

    task_id = str(uuid.uuid4())
    run = TaskRun(task=task, task_id=task_id)
    started = time.monotonic()

    spec = load_fixture(task.fixture)
    mf = fx.materialize(spec, eval_root / "work")
    # projects.json is rewritten per task because the fixture directories are
    # recreated per task -- the reviewer reads it fresh on every poll tick, so
    # it picks the new paths up without a restart.
    fx.write_projects_json(eval_root / "projects.json", [mf])
    from agent.config import reload_projects
    reload_projects()

    route, route_reason = _derive_route(task)
    from agent.outer_state import initial_state
    state = initial_state(
        task_id=task_id, goal=task.goal, repo=spec.name, budget_usd=task.budget_usd,
        # The operator's final look is what parks this before the merge. It is
        # the whole ship-depth decision, and it is one flag.
        require_merge_review=True,
        # Commands ARE auto-approved, and the first end-to-end run is why.
        # With approval required, every task parked on its first `bash` call
        # and the suite measured the operator's responsiveness rather than the
        # agent's coding. The interactive approval gate is a real feature and
        # it is simply not what a golden task is asking about.
        #
        # Safe here in a way it would not be on a real project: the command
        # runs in the Docker sandbox with --network none, against a fixture
        # repo in a temp directory that is deleted at the end of the run.
        auto_approve_commands=True,
        route=route, route_reason=route_reason,
    )

    thread_config = {"configurable": {"thread_id": task_id},
                     "metadata": {"task_id": task_id, "repo": spec.name, "eval": task.id},
                     "tags": ["eval", spec.name]}
    try:
        await asyncio.wait_for(graph.ainvoke(state, thread_config), timeout=TASK_TIMEOUT_S)
        final = await _final_state(graph, thread_config)
    except TimeoutError:
        run.outcome, run.error = "error", f"exceeded {TASK_TIMEOUT_S}s"
        final = await _final_state(graph, thread_config)
    except Exception as e:  # noqa: BLE001 -- one task blowing up must not cost
        # the eleven others already paid for. Recorded as an error outcome,
        # which never counts as a pass.
        logger.exception("eval task %s raised", task.id)
        run.outcome, run.error = "error", f"{type(e).__name__}: {e}"
        final = await _final_state(graph, thread_config)

    review = final.get("review_gate_result") or {}
    run.review_verdict = review.get("verdict")
    run.iteration_count = final.get("iteration_count")
    run.cost_usd = float(final.get("cost_so_far") or 0.0)
    if run.outcome != "error":
        run.outcome, run.escalation_reason = _read_outcome(final)
    # The gate only ever reaches a commit with a green suite, so a task that
    # produced a verdict passed its checks. A task that never got that far
    # leaves this None, and `checks_pass` assertions then read as
    # undetermined -- which scores as a failure, not a pass.
    if run.review_verdict:
        run.checks_pass = True
    elif run.outcome == "escalated" and "check" in (run.escalation_reason or "").lower():
        run.checks_pass = False
    # A blocked task is not an outcome: nothing was verified, nothing was
    # reviewed, and verify_and_ship wrote no episode for it either. Leaving
    # checks_pass as None is what makes its assertions read as undetermined,
    # which scores as a failure rather than a quiet pass.

    run.changed_paths = fx.changed_paths(mf, fx.task_branch_name(task_id))
    run.assertions = await evaluate(task.assertions, AssertionContext(
        worktree=mf.sandbox,
        changed_paths=run.changed_paths,
        checks_pass=run.checks_pass,
        review_verdict=run.review_verdict,
        iteration_count=run.iteration_count,
        run_command=run_command,
        project=spec.name,
    ))
    run.duration_s = time.monotonic() - started
    return run


async def run_suite(tasks: list[TaskSpec], *, graph, config, eval_root: Path,
                    cost_ceiling_usd: float, run_command=None,
                    on_task=None) -> SuiteRun:
    """Every task in order, until they run out or the money does."""
    suite = SuiteRun(started_at=time.time(), window_label=time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    for task in tasks:
        if task.skip:
            suite.runs.append(TaskRun(task=task, task_id="", outcome="skipped",
                                      error=task.skip))
            continue
        # Checked BEFORE starting, against the budget this task is allowed to
        # spend. Stopping after the overspend would report a ceiling that was
        # already breached, which is not a ceiling.
        if suite.total_cost + task.budget_usd > cost_ceiling_usd:
            suite.stopped_early = (
                f"stopped before {task.id}: ${suite.total_cost:.2f} spent and this task may "
                f"spend ${task.budget_usd:.2f}, which would cross the ${cost_ceiling_usd:.2f} ceiling")
            break
        run = await run_task(task, graph=graph, config=config, eval_root=eval_root,
                             run_command=run_command)
        suite.runs.append(run)
        suite.total_cost += run.cost_usd
        if on_task:
            on_task(run)
    return suite


def eval_config(config, eval_root: Path):
    """The same config, pointed at a throwaway SQLite database.

    This is the isolation that matters most. An eval writes an episode per
    task, and episodes are what agent/benchmarks.py computes the Analytics
    panel from -- so a run against the live store would inject a dozen
    synthetic tasks into the production numbers and skew the very figures it
    exists to explain. A separate DSN makes that structurally impossible
    rather than a rule someone has to remember.
    """
    return dataclasses.replace(config, dsn=f"sqlite:///{eval_root / 'eval.db'}")
