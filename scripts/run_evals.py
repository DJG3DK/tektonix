#!/usr/bin/env python3
"""Run the golden-task eval suite.

    scripts/run_evals.py --verify            # free: are the fixtures and specs sound?
    scripts/run_evals.py --dry-run           # free: what would run, and what would it cost?
    scripts/run_evals.py                     # the real thing, real money
    scripts/run_evals.py --only py-median    # one task
    scripts/run_evals.py --ceiling 10        # stop sooner

THE IMPORT ORDER IN main() IS LOAD-BEARING, and it is the reason this is a
script rather than a module with a __main__. Two things are read at import
time and can only be set before it:

  * AGENT_PROJECTS_JSON, which decides what agent.config.PROJECTS contains.
    Set late, the run would see the operator's real projects.
  * REVIEW_SERVICE_PORT / REVIEW_CONTROL_PORT, which agent.tools.review_gate
    binds into module constants. Set late, the run would send its commits to
    the LIVE reviewer -- which would review them against the live state file
    and bill the live usage log.

Neither failure is loud. Both are silent and wrong, which is why the imports
below are ordered deliberately and commented rather than hoisted to the top.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Safe to import now: none of these reach agent.config or agent.tools.
from agent.evals import fixtures as fx  # noqa: E402
from agent.evals import reviewer as ev_reviewer  # noqa: E402
from agent.evals import status as ev_status  # noqa: E402 -- imports agent.paths only
from agent.evals.spec import SpecError, load_fixture, load_suite  # noqa: E402

DEFAULT_CEILING_USD = 25.0


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", nargs="*", metavar="TASK_ID",
                   help="run only these tasks (default: all of them)")
    p.add_argument("--parallel", type=int, default=1, metavar="N",
                   help="run N tasks at once, each in its own workspace (default 1)")
    p.add_argument("--ceiling", type=float, default=DEFAULT_CEILING_USD,
                   help=f"stop once spend would cross this many dollars (default {DEFAULT_CEILING_USD})")
    p.add_argument("--dry-run", action="store_true",
                   help="validate the specs and print the plan; spend nothing")
    p.add_argument("--verify", action="store_true",
                   help="build every fixture, prove its checks are green, and prove each task's "
                        "assertions FAIL before the agent touches it; spend nothing")
    p.add_argument("--keep", action="store_true",
                   help="leave the working directory behind for inspection")
    p.add_argument("--notes", default="", help="a line recorded in the report")
    p.add_argument("--status-file", nargs="?", const="default", default=None,
                   help="write progress to a file the dashboard reads (logs/evals/status.json "
                        "unless a path is given); set by a run started from the dashboard")
    return p.parse_args(argv)


async def _sandboxed(cmd: str, cwd: str, timeout: int = 300, network: str = "none"):
    from agent.tools.sandbox import run_shell_sandboxed
    return await run_shell_sandboxed(cmd, cwd, timeout=timeout, network=network)


async def verify(tasks, root: Path) -> int:
    """Is this suite worth running?

    Two questions, and the second is the one that matters. Any suite can be
    green; the useful property is that it is not green BEFORE the agent does
    anything. An assertion that already passes on the pristine fixture tests
    nothing, and a task whose assertions all already pass is a task that will
    report success no matter how badly the agent behaves -- the most
    expensive kind of nothing, because it looks like a result.
    """
    # Registered exactly as a real run registers them, and before the first
    # import of agent.tools.sandbox. Its mount allow-list reads PROJECTS to
    # decide whether a worktree's .git may be mounted, so an unregistered
    # fixture fails closed -- correct, but it would mean --verify exercised a
    # containment path no real run takes, and printed a refusal per task
    # while doing it.
    materialized = [fx.materialize(load_fixture(name), root / "verify")
                    for name in sorted({t.fixture for t in tasks})]
    projects_json = root / "projects.json"
    fx.write_projects_json(projects_json, materialized)
    os.environ["AGENT_PROJECTS_JSON"] = str(projects_json)
    from agent.config import reload_projects
    reload_projects()
    by_name = {mf.name: mf for mf in materialized}

    from agent.evals.assertions import AssertionContext, evaluate

    problems: list[str] = []
    checked: set[str] = set()
    print("\nverifying fixtures and specs (no agent, no spend)")
    print("=" * 64)

    for task in tasks:
        spec = load_fixture(task.fixture)
        mf = by_name[spec.name]
        if spec.name not in checked:
            checked.add(spec.name)
            # The fixture must START green. A fixture whose suite already
            # fails makes every task on it indistinguishable from a task the
            # agent broke.
            result = await _sandboxed("npm test --silent", str(mf.sandbox))
            # ...and it must leave the worktree CLEAN. The commit gate refuses
            # build artifacts, so a fixture whose test run drops .pyc files
            # beside the source escalates a task the agent actually completed
            # -- and the report blames the agent. That is exactly how the
            # first end-to-end run failed, and it is free to detect here.
            dirty = fx.changed_paths(mf, None)
            state = "green" if result.get("ok") else "FAILING"
            print(f"  fixture {spec.name:10} checks {state}"
                  + (f", worktree DIRTY afterwards: {', '.join(dirty)}" if dirty else ", clean after"))
            if not result.get("ok"):
                problems.append(f"fixture {spec.name}: its own suite fails before any task runs\n"
                                + (result.get("output") or "")[-500:])
            if dirty:
                problems.append(
                    f"fixture {spec.name}: running its checks leaves {', '.join(dirty)} in the "
                    f"worktree. The commit gate refuses build artifacts, so every task on this "
                    f"fixture will escalate at the commit and the report will blame the agent. "
                    f"Add them to the fixture's .gitignore.")

        report = await evaluate(task.assertions, AssertionContext(
            worktree=mf.sandbox,
            changed_paths=(),
            # Nothing ran, so nothing is known. checks_pass/review_verdict
            # assertions read as undetermined here, which scores as a failure
            # -- correct: an unrun task has not passed its checks.
            run_command=_sandboxed,
            project=spec.name,
        ))
        # Only GOAL assertions are checked for vacuity. A guard is supposed to
        # hold already -- its job is to stay true, and one that failed here
        # would mean the fixture, not the agent, is wrong.
        goals = [r for r in report.results if not r.assertion.guard]
        guards = [r for r in report.results if r.assertion.guard]
        already = [r for r in goals if r.ok]
        broken_guards = [r for r in guards if not r.ok]
        mark = "ok  " if not (already or broken_guards) else "BAD "
        print(f"  {mark} {task.id:28} {len(goals) - len(already)}/{len(goals)} goal assertions "
              f"correctly fail, {len(guards) - len(broken_guards)}/{len(guards)} guards hold")
        for r in already:
            problems.append(f"{task.id}: goal assertion already passes before the agent runs "
                            f"-- {r.assertion.describe()} ({r.detail})")
        for r in broken_guards:
            problems.append(f"{task.id}: guard does not hold on the pristine fixture, so it can "
                            f"never pass -- {r.assertion.describe()} ({r.detail})")

    print("-" * 64)
    if problems:
        print(f"  {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"  {len(tasks)} task(s), {len(checked)} fixture(s): sound")
    return 0


def dry_run(tasks, ceiling: float) -> int:
    print(f"\n{len(tasks)} task(s) would run, ceiling ${ceiling:.2f}")
    print("=" * 64)
    # The real run checks ACTUAL spend so far plus the next task's cap, so a
    # task marked "if room" here is only skipped when the ones before it
    # really spent that much -- the worst case, not the expected one.
    spend = 0.0
    at_risk = 0
    for t in tasks:
        if t.skip:
            print(f"  SKIP {t.id:28} {t.skip}")
            continue
        room = spend + t.budget_usd <= ceiling
        spend += t.budget_usd
        at_risk += not room
        print(f"  {'run ' if room else 'room?'} {t.id:28} {t.category:14} {t.fixture:9} "
              f"${t.budget_usd:.2f}  {len(t.assertions)} assertion(s)")
    print("-" * 64)
    print(f"  caps add up to ${spend:.2f}; typical spend is a few cents a task")
    if at_risk:
        print(f"  {at_risk} task(s) marked room? run only if the ones before them leave room "
              f"under the ${ceiling:.2f} ceiling -- skipped only in the worst case")
    print()
    return 0


async def live_runtime_settings(live_config, open_store) -> str:
    """Load production's runtime settings, read-only, before the run switches
    to its own store.

    They live in the task store, and the run's store is a fresh SQLite file,
    so without this every run used the built-in defaults -- a 180s model-call
    timeout against production's 300s, among others -- and the suite measured
    an agent configured unlike the one it stands for. 2026-09-23: that
    difference failed a task whose test-writer calls ran for 4-9 minutes.
    Nothing is written; a store that cannot be read leaves the defaults and
    says so.
    """
    from agent import runtime_settings as rs
    try:
        async with open_store(live_config) as store:
            await rs.load(store)
    except Exception as e:  # noqa: BLE001 -- no live store (CI, a fresh box) is an answer
        return f"defaults (live store unreadable: {type(e).__name__})"
    return "production's (" + ", ".join(f"{k}={v:g}" for k, v in sorted(rs.all_values().items())
                                        if k.endswith("timeout_s")) + ")"


async def run(tasks, args, root: Path, status_path: Path | None = None) -> int:
    # --- 1. the fixtures, and the projects.json that is the ONLY one this run
    # can see. Written before anything reads AGENT_PROJECTS_JSON.
    materialized = [fx.materialize(load_fixture(name), root / "work")
                    for name in sorted({t.fixture for t in tasks})]
    projects_json = root / "projects.json"
    fx.write_projects_json(projects_json, materialized)
    os.environ["AGENT_PROJECTS_JSON"] = str(projects_json)

    # --- 2. our own reviewer pair, on free ports, against that file.
    print("starting an isolated reviewer pair...")
    rev = await ev_reviewer.start(projects_json, root / "reviewer")
    print(f"  reviewer on {rev.control_port}, dashboard on {rev.service_port} "
          f"(live pair on 4101/4100 untouched)")
    os.environ.update(rev.env_overrides)

    try:
        # --- 3. NOW the agent. Everything above had to happen first: this
        # import chain reads AGENT_PROJECTS_JSON and both port variables and
        # freezes them into module state.
        from agent.config import load_config
        from agent.evals import report as ev_report
        from agent.evals.runner import eval_config, run_suite
        from agent.graph import open_checkpointer, open_store
        from agent.outer_graph import build_outer_graph

        live = load_config()
        settings_note = await live_runtime_settings(live, open_store)
        print(f"  runtime settings: {settings_note}")
        config = eval_config(live, root)
        print(f"  store: {config.dsn}  (the live store is untouched)")

        def announce(r):
            mark = "PASS" if r.passed else "FAIL"
            print(f"  {mark}  {r.task.id:28} ${r.cost_usd:>6.2f}  {r.outcome}")
            if status_path is not None:
                ev_status.task_done(status_path, task_id=r.task.id, passed=r.passed,
                                    cost_usd=r.cost_usd, outcome=r.outcome)

        async with open_checkpointer(config) as checkpointer, open_store(config) as store:
            graph = build_outer_graph(config, checkpointer, store).compile(
                checkpointer=checkpointer, store=store)
            print(f"\nrunning {len(tasks)} task(s), {max(1, args.parallel)} at a time, "
                  f"ceiling ${args.ceiling:.2f}")
            print("=" * 64)
            suite = await run_suite(tasks, graph=graph, config=config, eval_root=root,
                                    cost_ceiling_usd=args.ceiling,
                                    run_command=_sandboxed, on_task=announce,
                                    parallel=max(1, args.parallel))

        previous = ev_report.latest_report()
        from agent import runtime_settings as rs
        report = ev_report.build(suite, cost_ceiling_usd=args.ceiling, notes=args.notes, only=args.only,
                                 runtime_settings=rs.all_values(), parallel=max(1, args.parallel))
        path = ev_report.write(report)
        args._report_path = str(path)
        print(ev_report.render(report))
        if previous:
            print(ev_report.diff_against(report, previous))
        print(f"\n  written to {path}\n")
        return 0 if report["tasks_passed"] == report["tasks_attempted"] else 1
    finally:
        await ev_reviewer.stop(rev)


def _status_path(args) -> Path | None:
    if not args.status_file:
        return None
    return ev_status.STATUS_PATH if args.status_file == "default" else Path(args.status_file)


async def main_async(argv=None) -> int:
    args = _parse_args(argv)
    try:
        tasks = load_suite(only=args.only)
    except SpecError as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 2

    if args.dry_run:
        return dry_run(tasks, args.ceiling)

    root = Path(tempfile.mkdtemp(prefix="tektonix-evals-"))
    try:
        if args.verify:
            return await verify(tasks, root)
        status_path = _status_path(args)
        if status_path is not None:
            ev_status.start(status_path, notes=args.notes, only=args.only, tasks_total=len(tasks))
        code = 1
        try:
            code = await run(tasks, args, root, status_path)
            return code
        finally:
            if status_path is not None:
                ev_status.finish(status_path, exit_code=code,
                                 report=getattr(args, "_report_path", None))
    finally:
        if args.keep:
            print(f"  working directory kept at {root}")
        else:
            fx.teardown(root)


def main(argv=None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
