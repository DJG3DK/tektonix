"""The fixed repository state a golden task runs against.

A fixture is an ordinary git repo -- `live/` with one commit, `sandbox/` as a
worktree of it on `agent-base` -- because that is exactly the shape every real
project has, and a fixture that were shaped differently would be measuring a
code path no production task takes.

IT IS REBUILT PER TASK, NOT RESET. Resetting is faster and it is the wrong
trade: a reset that misses something (a stray untracked file, a branch left
behind, a stale index) does not fail, it contaminates -- the next task starts
from a state no spec describes, and the number that comes out is wrong in a
way nobody can see. Rebuilding from files/ is a few hundred milliseconds and
has no such failure mode.

FIXTURES CARRY NO DEPENDENCIES. Checks run as `node --test` and
`python3 -m unittest`, both of which the sandbox image already has, so a run
needs no npm install, no package registry, and no network. A golden suite
whose result depends on whether a registry was up that morning is not a
benchmark.

AND EVERY FIXTURE CARRIES A .gitignore, which sounds like housekeeping and is
not. The commit gate refuses to commit build artifacts, so the first
end-to-end run escalated a task the agent had actually completed: unittest had
left four .pyc files beside the source it fixed. The report blamed the agent
for the fixture. `scripts/run_evals.py --verify` now runs each fixture's
checks and fails a fixture whose worktree is dirty afterwards.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent.evals.spec import FixtureSpec

# The branch a worktree sits on, matching what provisioning.create_worktree
# gives a real project. The reviewer compares a task branch against its
# merge-base with live, so this is also the base every diff is measured from.
BASE_BRANCH = "agent-base"

_GIT_ENV = {
    # A fixture commit must not depend on who is running the suite, or on
    # whether they have a git identity configured at all.
    "GIT_AUTHOR_NAME": "tektonix-evals",
    "GIT_AUTHOR_EMAIL": "evals@localhost",
    "GIT_COMMITTER_NAME": "tektonix-evals",
    "GIT_COMMITTER_EMAIL": "evals@localhost",
    # Neither should it depend on the operator's global git config -- a
    # `commit.gpgsign = true` out there would hang the whole suite on a
    # passphrase prompt.
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


class FixtureError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializedFixture:
    name: str
    live: Path
    sandbox: Path
    spec: FixtureSpec


def _git(args: list[str], cwd: Path) -> str:
    import os
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, env={**os.environ, **_GIT_ENV}, timeout=120)
    if proc.returncode != 0:
        raise FixtureError(f"git {' '.join(args)} in {cwd} failed: "
                           f"{(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def materialize(spec: FixtureSpec, root: Path) -> MaterializedFixture:
    """Build `<root>/live/<name>` and `<root>/sandbox/<name>` from scratch."""
    live = root / "live" / spec.name
    sandbox = root / "sandbox" / spec.name
    for path in (live, sandbox):
        if path.exists():
            shutil.rmtree(path)
    live.parent.mkdir(parents=True, exist_ok=True)
    sandbox.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(spec.files_dir, live)
    _git(["init", "-q", "-b", "main"], live)
    _git(["add", "-A"], live)
    _git(["commit", "-q", "-m", f"fixture {spec.name}: initial state"], live)
    # A worktree, not a clone -- see the module docstring. The per-task branch
    # a commit lands on is then a plain local ref in `live`, which is what
    # lets the reviewer read it with no remote in between.
    _git(["worktree", "add", "-q", str(sandbox), "-b", BASE_BRANCH], live)
    return MaterializedFixture(name=spec.name, live=live, sandbox=sandbox, spec=spec)


def projects_entry(mf: MaterializedFixture) -> dict:
    """This fixture as a projects.json entry.

    The `review` block comes straight from fixture.yaml and is what the
    reviewer reads its check commands from -- the fixture goes through the
    reviewer as an ordinary project, with nothing special-cased for it
    anywhere downstream.
    """
    entry = {"live": str(mf.live), "sandbox": str(mf.sandbox)}
    if mf.spec.review:
        entry["review"] = json.loads(json.dumps(mf.spec.review))  # a copy, not the spec's dict
    return entry


def write_projects_json(path: Path, fixtures: list[MaterializedFixture]) -> None:
    """The eval's own projects.json.

    Its own, and never the live one. The agent is not allowed to write
    projects.json at all (that rule is what stops a model granting its own
    code network egress), and a harness that edited the real file to add a
    fixture would be doing on the operator's behalf the exact thing the rule
    exists to prevent -- with the added hazard that a crash mid-run leaves the
    entry behind. AGENT_PROJECTS_JSON already exists for the container bundle;
    this reuses it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects": {mf.name: projects_entry(mf) for mf in fixtures}}
    path.write_text(json.dumps(payload, indent=2) + "\n")


def changed_paths(mf: MaterializedFixture, task_branch: str | None) -> tuple[str, ...]:
    """What the agent actually changed, repo-relative and posix-separated.

    Two sources, because a task can end in either state. If it committed, the
    truth is the branch against the base it forked from. If it did not, the
    truth is the dirty worktree -- and that case has to be covered or every
    diff assertion on an escalated task would report "changed nothing" and
    quietly pass a `diff_excludes`.
    """
    if task_branch:
        try:
            out = _git(["diff", "--name-only", f"{BASE_BRANCH}...{task_branch}"], mf.live)
            paths = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if paths:
                return tuple(sorted(paths))
        except FixtureError:
            pass  # branch never created: fall through to the worktree
    out = _git(["status", "--porcelain", "--untracked-files=all"], mf.sandbox)
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            # `XY path` and, for a rename, `XY old -> new`; the new name is
            # what a spec's globs are written against.
            paths.append(line[3:].split(" -> ")[-1].strip().strip('"'))
    return tuple(sorted(set(paths)))


# How much of a failing task's diff to keep. Enough to read what it actually
# did; not so much that a report full of failures becomes a tarball.
MAX_DIFF_CHARS = 20_000


def changed_diff(mf: MaterializedFixture, task_branch: str | None) -> str:
    """The actual diff, for a task whose assertions failed.

    The first full run is why this exists. py-top-n-heap failed a guard that
    said the test file must not change, and the report recorded only that
    `tests/test_report.py` was among the changed paths -- so there was no way
    to tell whether the agent had ADDED a test or WEAKENED one, which are
    opposite findings. By then the fixture had been torn down and there was
    nothing left to look at.

    A path list says a rule was broken. The diff says what the agent did, and
    that is the whole reason anybody reads a failing golden task.
    """
    try:
        if task_branch:
            try:
                out = _git(["diff", f"{BASE_BRANCH}...{task_branch}"], mf.live)
                if out.strip():
                    return out[:MAX_DIFF_CHARS]
            except FixtureError:
                pass
        # Uncommitted work, for a task that escalated before committing.
        out = _git(["diff", "HEAD"], mf.sandbox)
        return out[:MAX_DIFF_CHARS]
    except FixtureError:
        return ""


def task_branch_name(task_id: str) -> str:
    """What verify_and_ship will name the branch for this task.

    Mirrors agent/tools/git.py rather than being derived independently: the
    reviewer only ever reviews a branch matching `agent/<uuid>`, so a harness
    that guessed a different shape would look for a diff that is there under
    another name and report every task as having changed nothing.
    """
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(task_id)).strip("-.") or "task"
    return f"agent/{safe}"


def teardown(root: Path) -> None:
    """Remove everything a run created. Never raises: a suite that has just
    produced a report must not lose it to a failed rmdir."""
    try:
        shutil.rmtree(root, ignore_errors=True)
    except OSError:  # pragma: no cover - ignore_errors already covers it
        pass
