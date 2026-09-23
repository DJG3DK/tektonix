"""What a run produced, as JSON to keep and as a table to read.

Two audiences and one source. The JSON is the record: it goes in
logs/evals/<run>.json so two runs a fortnight apart can be diffed, which is
the entire point of a fixed suite. The table is for the person who just spent
twenty-five dollars and wants to know, in one screen, whether the change they
made helped.

The aggregate half is deliberately the SAME six numbers agent/benchmarks.py
computes for production tasks, derived the same way from the same episode
records. An eval that scored itself on its own private metrics would answer a
question the dashboard cannot be compared against, and comparing is what this
is for.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from agent import paths
from agent.benchmarks import summarise_episodes
from agent.evals.runner import SuiteRun, TaskRun

REPORT_DIR = paths.REPO_ROOT / "logs" / "evals"


def _assertion_rows(run: TaskRun) -> list[dict]:
    return [{
        "kind": r.assertion.kind,
        "describe": r.assertion.describe(),
        "ok": r.ok,
        "undetermined": r.undetermined,
        "detail": r.detail,
    } for r in run.assertions.results]


def task_row(run: TaskRun) -> dict:
    return {
        "id": run.task.id,
        "fixture": run.task.fixture,
        "category": run.task.category,
        "task_id": run.task_id,
        "passed": run.passed,
        "outcome": run.outcome,
        "escalation_reason": run.escalation_reason,
        "review_verdict": run.review_verdict,
        "checks_pass": run.checks_pass,
        # Redos, not passes -- the same convention as the episode record and
        # agent/benchmarks.py. 0 means it landed first time.
        "iterations": run.iteration_count,
        "cost_usd": round(run.cost_usd, 4),
        "duration_s": round(run.duration_s, 1),
        "changed_paths": list(run.changed_paths),
        "diff": run.diff,
        "assertions": _assertion_rows(run),
        "error": run.error,
    }


def as_episodes(suite: SuiteRun) -> list[dict]:
    """The run's tasks in the shape agent/benchmarks.py reads.

    Built from the TaskRuns rather than read back out of the eval store: the
    two agree by construction (both come from the same final state), and
    building them here means the aggregate can be computed for a run that was
    cut short before its store was ever opened -- which is exactly the run
    somebody most wants the numbers for.
    """
    return [{
        "outcome": r.outcome,
        "review_verdict": r.review_verdict,
        "iteration_count": r.iteration_count,
        "cost_usd": r.cost_usd,
        "timestamp": suite.window_label,
    } for r in suite.attempted if r.outcome not in ("error", "blocked")]


def build(suite: SuiteRun, *, cost_ceiling_usd: float, notes: str = "",
          only: list[str] | None = None, runtime_settings: dict | None = None,
          parallel: int = 1) -> dict:
    attempted = suite.attempted
    # "error" is the harness failing and "blocked" is a task that never
    # started; neither says anything about the agent, and counting either as
    # an escalation would blame the agent for something else.
    scored = [r for r in attempted if r.outcome not in ("error", "blocked")]
    by_category: dict[str, dict] = {}
    for r in attempted:
        bucket = by_category.setdefault(r.task.category, {"tasks": 0, "passed": 0})
        bucket["tasks"] += 1
        bucket["passed"] += int(r.passed)

    return {
        "schema": 1,
        "started_at": suite.window_label,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": notes,
        # Which tasks were asked for: [] is the whole suite. The dashboard
        # headlines the latest FULL run, and a one-task diagnostic run must
        # not become "the score".
        "only": list(only or []),
        # The dashboard's runtime knobs the agent ran under (timeouts, loop
        # limits). A score is only comparable with another taken under the
        # same ones.
        "runtime_settings": dict(runtime_settings or {}),
        # How many tasks ran at once. Each has its own workspace, so this
        # should not move the pass rate -- which is exactly why it is recorded.
        "parallel": parallel,
        "cost_ceiling_usd": cost_ceiling_usd,
        "total_cost_usd": round(suite.total_cost, 4),
        "stopped_early": suite.stopped_early,
        "tasks_total": len(suite.runs),
        "tasks_attempted": len(attempted),
        "tasks_passed": suite.passed,
        # The headline. Of the tasks that actually ran, how many did the thing
        # the spec asked for -- assertions, not outcome.
        "pass_rate": round(100.0 * suite.passed / len(attempted), 1) if attempted else None,
        "by_category": by_category,
        # The same six numbers the Analytics panel shows for production, so a
        # run is directly comparable to the fortnight it was run in.
        "benchmarks": summarise_episodes(as_episodes(suite)) if scored else None,
        "tasks": [task_row(r) for r in suite.runs],
    }


def write(report: dict, directory: Path | None = None) -> Path:
    directory = directory or REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report['started_at'].replace(':', '-')}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    return path


# --- the readable half -----------------------------------------------------

_TICK, _CROSS, _DASH = "PASS", "FAIL", " -- "


def render(report: dict) -> str:
    """One screen. Failures carry their reason inline, because the whole point
    of a failing golden task is what it says about the change that broke it --
    and a table that only shows a cross sends the reader to the JSON."""
    lines = [
        "",
        f"Golden eval — {report['started_at']}",
        "=" * 64,
    ]
    if report["notes"]:
        lines.append(report["notes"])

    for row in report["tasks"]:
        if row["outcome"] == "skipped":
            lines.append(f"  SKIP  {row['id']:28} {row['error']}")
            continue
        mark = _TICK if row["passed"] else _CROSS
        detail = f"{row['outcome']}"
        if row["review_verdict"]:
            detail += f"/{row['review_verdict']}"
        if row["iterations"] is not None:
            detail += f", {row['iterations']} redo(s)"
        lines.append(f"  {mark}  {row['id']:28} ${row['cost_usd']:>6.2f}  "
                     f"{row['duration_s']:>6.0f}s  {detail}")
        if row["error"]:
            lines.append(f"          {row['error']}")
        for a in row["assertions"]:
            if not a["ok"]:
                why = "could not evaluate" if a["undetermined"] else a["detail"]
                lines.append(f"          x {a['describe']}")
                lines.append(f"            {why}")

    lines.append("-" * 64)
    passed, attempted = report["tasks_passed"], report["tasks_attempted"]
    rate = report["pass_rate"]
    lines.append(f"  {passed}/{attempted} passed"
                 + (f" ({rate:.0f}%)" if rate is not None else "")
                 + f"   ${report['total_cost_usd']:.2f} of ${report['cost_ceiling_usd']:.2f}")

    bench = report.get("benchmarks")
    if bench:
        first_pass = bench["first_pass_rate"]
        lines.append(
            "  first-pass reviews "
            + (f"{first_pass:.0f}% ({bench['first_pass']}/{bench['reviewed']})"
               if first_pass is not None else "n/a")
            + f"   redos median {bench['iterations_median']}"
            + f"   escalations {bench['escalated']}/{bench['tasks']}")

    if report["stopped_early"]:
        lines.append(f"  ! {report['stopped_early']}")
    lines.append("")
    return "\n".join(lines)


def diff_against(report: dict, previous: dict) -> str:
    """This run against an earlier one.

    A pass rate on its own is a number; against the last run it is a result.
    Tasks are matched by id, which is why spec.py insists an id never drifts
    from its filename -- a renamed task would read here as one task vanishing
    and another appearing, rather than as the same task changing its answer.
    """
    before = {t["id"]: t for t in previous.get("tasks", [])}
    now = {t["id"]: t for t in report.get("tasks", [])}
    broke = sorted(i for i in now if now[i]["passed"] is False
                   and before.get(i, {}).get("passed") is True)
    fixed = sorted(i for i in now if now[i]["passed"] is True
                   and before.get(i, {}).get("passed") is False)
    lines = [f"  vs {previous.get('started_at', 'an earlier run')}: "
             f"{previous.get('tasks_passed')}/{previous.get('tasks_attempted')} passed, "
             f"${previous.get('total_cost_usd', 0):.2f}"]
    if broke:
        lines.append(f"  REGRESSED: {', '.join(broke)}")
    if fixed:
        lines.append(f"  now passing: {', '.join(fixed)}")
    if not broke and not fixed:
        lines.append("  same tasks passing as last time")
    return "\n".join(lines)


def latest_report(directory: Path | None = None) -> dict | None:
    directory = directory or REPORT_DIR
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("*.json"))
    for path in reversed(files):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # a truncated report is not a reason to lose the comparison
    return None


__all__ = ["build", "write", "render", "diff_against", "latest_report", "as_episodes",
           "task_row", "asdict"]
