"""The task lifecycle: every resting state, every action that moves a task out
of one, and exactly what each action writes.

Before this module the transitions lived inside five endpoints, each building
its own state patch. Every failure found on 2026-09-23 was in a jump between
states rather than in the agent's work: a resume that woke on another task's
checkout, an approval that survived new work and shipped the old commit past
it, a re-ship that went to a paid work pass. Each was found live. Here they
are one table, and tests/test_lifecycle.py walks every cell of it and checks
the invariants that were broken -- so the next one fails in CI.

The functions are pure: they take the checkpoint's values (and the store's
status) and return a Transition, or raise Refused. Applying it -- the
checkpoint write, the audit record, starting the run -- stays with the
caller, because that is I/O and this is policy.

How a transition routes. `as_node` is the node LangGraph treats as having
just run; the graph continues from that node's outgoing edge:

  * "work"            -> next is verify_and_ship, unconditionally. For
                         anything that should re-enter the gate WITHOUT a
                         model call: a hand edit, a heal.
  * "verify_and_ship" -> next is whatever _route_after_verify says about the
                         patched state: "work" when there is feedback to act
                         on, "verify_and_ship" for an approved commit, END for
                         a resting state.
  * None              -> the checkpoint's own next node, untouched. Only for
                         a run that was cut off mid-flight (orphaned, stopped,
                         error) and should simply carry on.
"""
from __future__ import annotations

from agent.harness_voice import HARNESS, OPERATOR, operator

from dataclasses import dataclass, field

# Resting states, as the operator sees them. The first three come from the
# checkpoint; the rest from the store's status (a task can be "done" with
# nothing in the checkpoint saying so).
PHASES = ("running", "queued", "awaiting_approval", "awaiting_merge",
          "escalated", "done", "stopped", "error")

ACTIONS = ("resume", "merge_approve", "merge_request_changes",
           "command_decision", "operator_edit", "heal", "conclude_landed")

# Which phase each action may start from. tests/test_lifecycle.py fails if an
# action is added without a row here, and walks every phase x action pair.
ALLOWED: dict[str, frozenset[str]] = {
    # "running" is the orphan case: the store says running and nothing drives
    # it (the endpoint has already refused a task that is really running).
    "resume": frozenset({"escalated", "done", "stopped", "error", "running"}),
    "merge_approve": frozenset({"awaiting_merge"}),
    "merge_request_changes": frozenset({"awaiting_merge"}),
    "command_decision": frozenset({"awaiting_approval"}),
    "operator_edit": frozenset({"awaiting_merge"}),
    # Only ever the supervisor, and only for an infrastructure escalation --
    # which is the supervisor's judgement to make, not this table's.
    "heal": frozenset({"escalated"}),
    # The task's commits are already on main -- merged by hand, rebased and
    # landed by someone else, or merged on GitHub. Waiting on it is waiting
    # for nothing.
    "conclude_landed": frozenset({"escalated", "awaiting_merge"}),
}


class Refused(Exception):
    """An action this state does not allow. `status` is the HTTP answer."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Transition:
    patch: dict
    as_node: str | None
    # Delivered through the task's mailbox (agent/messages.py) rather than
    # the patch: the orphan resume must not touch routing, and the mailbox is
    # how a message reaches a run that is simply carrying on.
    mailbox_message: str | None = None
    summary: str = ""
    extra: dict = field(default_factory=dict)


def phase_of(values: dict, store_status: str | None) -> str:
    if values.get("escalated"):
        return "escalated"
    if values.get("pending_approval"):
        return "awaiting_approval"
    if values.get("pending_merge_approval"):
        return "awaiting_merge"
    return store_status or "unknown"


def _require(action: str, values: dict, store_status: str | None) -> str:
    phase = phase_of(values, store_status)
    if phase not in ALLOWED[action]:
        raise Refused(409, _REFUSALS.get(action, "not allowed now") + f" (task is {phase})")
    return phase


_REFUSALS = {
    "resume": "nothing to resume",
    "merge_approve": "task is not awaiting a merge decision",
    "merge_request_changes": "task is not awaiting a merge decision",
    "command_decision": "task has no pending approval request",
    "operator_edit": "edits can only be saved while the task is waiting for your final look",
    "heal": "only an escalated task can be healed",
    "conclude_landed": "only a parked task can be concluded",
}

# Anything that starts a fresh attempt starts these over: a streak carried
# across an operator's decision gives the new attempt no runway at all.
_FRESH = {"no_diff_streak": 0, "stale_pending_review_streak": 0}


def resume(values: dict, store_status: str | None, *, message: str | None,
           additional_budget: float) -> Transition:
    phase = _require("resume", values, store_status)
    if phase == "done" and not message:
        # Nothing to nudge with: reopening would re-run the same
        # investigation and land on the same premature "done".
        raise Refused(400, "resuming a done task requires a message telling it what to do next")

    new_budget = values["budget_usd"] + additional_budget
    # +40 per resume, the same way budget grows: every work/verify cycle of
    # the task's whole life counts against max_iterations.
    new_max = values.get("max_iterations", 40) + 40
    base = {"budget_usd": new_budget, "max_iterations": new_max}
    extra = {"new_budget_usd": new_budget, "new_max_iterations": new_max}

    approved = values.get("merge_approved_sha")
    if phase == "escalated" and approved and approved == values.get("committed_sha") and not message:
        # Approved, committed, and escalated only on the way out. The work is
        # finished; it goes back to shipping, not to a paid work pass.
        return Transition({**base, "escalated": False, "escalation_reason": None,
                           "stale_pending_review_streak": 0},
                          "verify_and_ship", summary="re-ship the approved commit", extra=extra)

    if phase == "escalated":
        budget_note = (
            f"Additional budget granted -- ${additional_budget:.2f} more, ${new_budget:.2f} total now. "
            if additional_budget > 0 else "No additional budget added. "
        )
        note = HARNESS + " " + (
            f"Resumed by operator after escalation (was: {values.get('escalation_reason') or 'unknown reason'}). "
            f"{budget_note}Continue the task from where you left off."
        )
        if message:
            note += f"\n\n{operator(message)}"
        # New work means a new commit; an approval of the old one must not
        # survive to ship it.
        return Transition({**base, **_FRESH, "escalated": False, "escalation_reason": None,
                           "pending_feedback": note, "merge_approved_sha": None},
                          "verify_and_ship", summary="back to work", extra=extra)

    if phase == "done":
        return Transition({**base, "no_diff_streak": 0, "pending_feedback": operator(message),
                           "merge_approved_sha": None},
                          "verify_and_ship", summary="reopened with a note", extra=extra)

    # Orphaned, stopped or errored: carry on exactly where it was.
    return Transition(base, None, mailbox_message=message or None, summary="carry on", extra=extra)


def merge_decision(values: dict, store_status: str | None, decision: str,
                   message: str | None) -> Transition:
    if decision not in ("approve", "request_changes"):
        raise Refused(400, "decision must be 'approve' or 'request_changes'")
    _require("merge_approve" if decision == "approve" else "merge_request_changes", values, store_status)
    pending = values["pending_merge_approval"]
    if decision == "approve":
        # The EXACT sha the operator was shown: verify_and_ship merges only
        # when this equals the outstanding commit, so an approval can never
        # ship a later one.
        return Transition({"merge_approved_sha": pending["sha"], "pending_merge_approval": None},
                          "verify_and_ship", summary="approved", extra={"sha": pending["sha"]})
    if not (message or "").strip():
        raise Refused(400, "request_changes requires a message -- the agent needs to know what to change")
    return Transition({**_FRESH, "pending_merge_approval": None, "merge_approved_sha": None,
                       "pending_feedback": (
                           HARNESS + " The operator reviewed the final diff and sent it back for more work "
                           "before it may merge. Their notes:\n\n" + operator(message))},
                      "verify_and_ship", summary="sent back")


def command_decision(values: dict, store_status: str | None, decision: str,
                     message: str | None) -> Transition:
    """One operator decision, applied to EVERY pending action request.

    An assumption, stated so it cannot be broken quietly: the dashboard shows
    one approval card per interrupt, and the model virtually always proposes
    one gated call at a time -- so one click answers one card, fanned out as
    N identical decisions. If a turn ever queues two DIFFERENT gated calls,
    this would answer both with the one click. tests/test_lifecycle.py pins
    the fan-out, so a per-action approval UI has to change this function on
    purpose rather than inherit it (2026-09-23 follow-up review, F10).
    """
    _require("command_decision", values, store_status)
    count = len((values["pending_approval"] or {}).get("action_requests") or [])
    if decision == "approve":
        decisions = [{"type": "approve"} for _ in range(count)]
    elif decision == "respond":
        # The answer to ask_user. A respond without text is meaningless.
        if not (message or "").strip():
            raise Refused(400, "respond decision requires a message (the answer)")
        decisions = [{"type": "respond", "message": message} for _ in range(count)]
    else:
        reject = {"type": "reject"}
        if message:
            reject["message"] = message
        decisions = [dict(reject) for _ in range(count)]
    # pending_feedback is a routing placeholder, never sent to the model:
    # work_node reads approval_decision first and resumes the paused turn.
    return Transition({"pending_approval": None, "approval_decision": decisions,
                       "pending_feedback": OPERATOR + " submitted an approval decision"},
                      "verify_and_ship", summary=f"command {decision}", extra={"count": count})


def operator_edit(values: dict, store_status: str | None, *, base_sha: str, files: list[dict],
                  note: str | None, by: str) -> Transition:
    _require("operator_edit", values, store_status)
    if base_sha != values["pending_merge_approval"].get("sha"):
        raise Refused(409, "the task has a newer commit than the one you edited -- reopen the file")
    return Transition({**_FRESH,
                       "operator_edits": {"base_sha": base_sha, "files": files, "note": note, "by": by},
                       "pending_merge_approval": None, "merge_approved_sha": None},
                      "work", summary="hand edit")


def heal(values: dict, store_status: str | None, *, reason: str, attempt: int,
         stage: str) -> Transition:
    """Put a task escalated by an infrastructure failure back on its way.

    stage="gate": the failure came after the work was done (the reviewer,
    a merge, a push). as_node="work", so the next node is verify_and_ship
    with no model call: an approved commit takes the fast path to ship, an
    unapproved one is re-reviewed, a branch whose base moved is rebased.
    There is no pending_feedback because nothing about the work was wrong.

    stage="work": the failure cut a work pass off (a dropped model
    connection). The pass has to be finished, so it goes back to work with a
    note saying why -- and any approval is void, since what ships next is
    new.
    """
    _require("heal", values, store_status)
    base = {**_FRESH, "escalated": False, "escalation_reason": None}
    if stage == "gate":
        return Transition(base, "work", summary=f"auto-heal #{attempt}: {reason}")
    if stage == "work":
        return Transition({**base, "merge_approved_sha": None, "pending_feedback": (
            f"Your previous pass was cut off by an infrastructure failure ({reason}) -- nothing "
            "you did caused it, and it has cleared. Continue the task from where you left off.")},
            "verify_and_ship", summary=f"auto-heal #{attempt}: {reason}")
    raise ValueError(f"unknown heal stage {stage!r}")


def conclude_landed(values: dict, store_status: str | None) -> Transition:
    """The work is on main already; close the task instead of waiting on it."""
    _require("conclude_landed", values, store_status)
    return Transition({"escalated": False, "escalation_reason": None, "pending_merge_approval": None,
                       "merge_approved_sha": None, "pending_feedback": None},
                      "verify_and_ship", summary="work already on main")
