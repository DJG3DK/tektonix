"""Did a change to this agent make it better, and at what cost?

Everything else in Analytics answers "what happened": spend by day, model
usage, which tasks ran. This answers a different question, and it is the one
a change is judged by -- whether tasks are landing on the first review rather
than the third, whether they cost less to get there, and whether the
retrieval subsystems are being used or merely paid for.

WHY A COMPARISON AND NOT A NUMBER. A first-pass rate of 60% means nothing on
its own; 60% against 45% last fortnight means something. So every metric is
computed over a window AND over the window immediately before it, of the same
length, and the delta is what is shown. That is also the honest shape for a
system with few tasks: a number that moved from 4/7 to 5/8 should be read as
noise, and showing both counts is what lets a reader see that.

WHERE THE NUMBERS COME FROM. Episodes -- one written per terminal outcome by
verify_and_ship -- carry outcome, iteration_count, cost_usd and the review
verdict. logs/retrieval_events.jsonl carries what memory and history search
offered and what was actually read. Nothing here calls a model or reaches the
network; it is two reads and some arithmetic, so it can be recomputed on
every request the way the rest of Analytics is.

WHAT IT DELIBERATELY DOES NOT DO. It does not score a task's output quality.
Whether the code was good is what the review gate is for, and a metric that
guessed at it would be the most quoted and least trustworthy number here.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent import episode_recall
from agent.store_paging import all_items

# An episode counts as landing first time when the review said READY and the
# task never went round again.
#
# ZERO, not one. agent/nodes/verify_and_ship.py records
# `state["iteration_count"]` as it stands at the terminal pass, and that
# counter is only ever incremented by `_loop_back` -- so it is a count of
# REDOS, not of passes, and a task that was written once and passed review
# records 0. Live data agrees: 71 of the shipped/READY episodes sit at 0 and
# the next bucket down is 1. Setting this to 1 silently folds every
# one-redo task into the headline number.
FIRST_PASS_ITERATIONS = 0


def _pct(part: int, whole: int) -> float | None:
    """A percentage, or None when there is nothing to divide by.

    None rather than 0.0 on purpose: "no tasks ran" and "no tasks passed" are
    different answers, and a dashboard that renders the first as 0% invites
    somebody to go looking for a regression that did not happen.
    """
    return round(100.0 * part / whole, 1) if whole else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 4)


def _p90(values: list[float]) -> float | None:
    """The tail, because a median hides the task that took nine rounds -- and
    the tail is where the cost and the frustration actually are."""
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, math.ceil(0.9 * len(s)) - 1)], 4)


def _ts(value: Any) -> float | None:
    """An episode's timestamp as epoch seconds, tolerantly.

    Episodes are written by a long-lived system and their timestamps have to
    be read, not trusted: a record whose stamp cannot be parsed is dropped
    from the window rather than being counted in whichever window zero falls
    into.
    """
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _episode_body(value: Any) -> dict | None:
    """The record, whichever shape it was stored in.

    agent/episodes.py writes through StoreBackend, so the value is a document
    with the record JSON in `content`. Older rows are the record itself. Both
    have to read here or the history simply shortens.
    """
    if not isinstance(value, dict):
        return None
    if "content" in value:
        try:
            body = json.loads(value["content"])
        except (TypeError, ValueError):
            return None
        return body if isinstance(body, dict) else None
    return value


def summarise_episodes(episodes: list[dict]) -> dict:
    """The task-outcome half, over episodes already filtered to a window."""
    outcomes: dict[str, int] = {}
    iterations: list[float] = []
    costs: list[float] = []
    shipped_costs: list[float] = []
    first_pass = 0
    reviewed = 0

    for e in episodes:
        outcome = str(e.get("outcome") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

        it = e.get("iteration_count")
        if isinstance(it, int | float):
            iterations.append(float(it))
        cost = e.get("cost_usd")
        if isinstance(cost, int | float):
            costs.append(float(cost))
            if outcome == "shipped":
                shipped_costs.append(float(cost))

        # Only episodes that actually reached a review can answer "did it pass
        # first time". A task that escalated before submitting anything is not
        # a first-pass failure; it never asked.
        verdict = str(e.get("review_verdict") or "")
        if verdict:
            reviewed += 1
            if verdict.upper() == "READY" and float(it or 0) <= FIRST_PASS_ITERATIONS:
                first_pass += 1

    total = len(episodes)
    shipped = outcomes.get("shipped", 0)
    escalated = outcomes.get("escalated", 0)
    return {
        "tasks": total,
        "shipped": shipped,
        "escalated": escalated,
        "outcomes": outcomes,
        "ship_rate": _pct(shipped, total),
        "escalation_rate": _pct(escalated, total),
        # The headline. Of the tasks that reached a review, how many were
        # right the first time.
        "first_pass_rate": _pct(first_pass, reviewed),
        "reviewed": reviewed,
        "first_pass": first_pass,
        "iterations_median": _median(iterations),
        "iterations_p90": _p90(iterations),
        "cost_median": _median(costs),
        "cost_p90": _p90(costs),
        "cost_per_shipped_median": _median(shipped_costs),
        "total_cost": round(sum(costs), 4),
    }


def summarise_retrieval(events: list[dict]) -> dict:
    """Whether the retrieval subsystems earn what they cost.

    Memory disclosure and history search were both built on an argument
    rather than a measurement. These are the measurements: how often an
    indexed memory section is actually fetched, and how often a history
    search leads to a record being opened. A section nobody reads is a
    section that should have been pinned or dropped; a search nobody follows
    is a search that is not answering the question.
    """
    offered_sections = 0
    read_sections = 0
    offered_events = 0
    queries = 0
    uses = 0

    for ev in events:
        kind = ev.get("event")
        if kind == "memory_offered":
            offered_events += 1
            offered_sections += len(ev.get("sections") or [])
        elif kind == "memory_read":
            read_sections += 1
        elif kind == "query":
            queries += 1
        elif kind == "use":
            uses += 1

    return {
        "memory_prompts": offered_events,
        "sections_offered": offered_sections,
        "sections_read": read_sections,
        # Per PROMPT, not per section: "how often did a task need to go and
        # fetch something" is the question, and dividing by every section
        # offered would bury it under the index size.
        "section_reads_per_prompt": round(read_sections / offered_events, 2) if offered_events else None,
        "history_queries": queries,
        "history_used": uses,
        # Of the searches that ran, how many led to a record being opened.
        # The number the vector leg has to justify itself against.
        "history_follow_rate": _pct(uses, queries),
    }


def _read_retrieval_events(since: float, until: float, path: Path | None = None) -> list[dict]:
    # episode_recall owns the path, env override and all -- deriving it a
    # second time here is how the two quietly end up pointing at different
    # files the day someone sets AGENT_RETRIEVAL_LOG.
    p = path or episode_recall.LOG_PATH
    if not p.is_file():
        return []
    out = []
    try:
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue  # a half-written line is not a reason to lose the file
                ts = ev.get("ts")
                if isinstance(ts, int | float) and since <= ts < until:
                    out.append(ev)
    except OSError:
        return []
    return out


def _delta(now: dict, before: dict, keys: list[str]) -> dict:
    """What moved, and only for metrics where both windows have a value.

    A delta against None is not zero, it is unknown, and rendering it as 0
    would show "no change" for a metric that had no data to change.
    """
    out = {}
    for k in keys:
        a, b = now.get(k), before.get(k)
        if isinstance(a, int | float) and isinstance(b, int | float):
            out[k] = round(a - b, 4)
    return out


DELTA_KEYS = [
    "first_pass_rate", "ship_rate", "escalation_rate",
    "iterations_median", "iterations_p90",
    "cost_median", "cost_per_shipped_median",
    "section_reads_per_prompt", "history_follow_rate",
]


async def benchmark_summary(store, repos: list[str], namespace_for, *,
                            window_days: int = 14, now: float | None = None,
                            retrieval_log: Path | None = None) -> dict:
    """Both windows, their metrics, and what moved between them.

    `namespace_for` is passed in rather than imported so this module does not
    depend on agent/deep_agent.py -- it is arithmetic over records, and a
    dependency on the agent build would make it unimportable in exactly the
    contexts that want to measure.
    """
    now = now if now is not None else datetime.now(tz=UTC).timestamp()
    span = window_days * 86400.0
    cur_from, prev_from = now - span, now - 2 * span

    current: list[dict] = []
    previous: list[dict] = []
    for repo in repos:
        for item in await all_items(store, namespace_for(repo)):
            body = _episode_body(item.value)
            if body is None:
                continue
            ts = _ts(body.get("timestamp"))
            if ts is None:
                continue
            if cur_from <= ts < now:
                current.append(body)
            elif prev_from <= ts < cur_from:
                previous.append(body)

    cur = {**summarise_episodes(current),
           **summarise_retrieval(_read_retrieval_events(cur_from, now, retrieval_log))}
    prev = {**summarise_episodes(previous),
            **summarise_retrieval(_read_retrieval_events(prev_from, cur_from, retrieval_log))}

    return {
        "window_days": window_days,
        "current": cur,
        "previous": prev,
        "delta": _delta(cur, prev, DELTA_KEYS),
        # Said plainly rather than left for the reader to work out: with a
        # handful of tasks a moved percentage is noise, and the dashboard
        # should say so rather than draw an arrow.
        "sample_warning": (
            "too few tasks for a meaningful comparison"
            if cur.get("tasks", 0) < 10 or prev.get("tasks", 0) < 10 else None
        ),
    }
