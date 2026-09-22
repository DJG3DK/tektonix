"""Did the agent actually do the thing?

The outcome alone cannot answer that. A task that ships, passes its checks and
gets a READY verdict has cleared every gate this system has, and can still
have "fixed" the bug by deleting the failing test. These are the predicates
that catch it: objective, evaluated by a script, and identical on every run.

The one that needs care is `command`. It runs against a worktree the agent
just wrote, which makes it agent-authored code by any useful definition, so it
runs through run_shell_sandboxed -- the same containment the check suite uses
-- and never on the host. An eval harness that shelled out directly would
reintroduce, in the name of measurement, precisely the privilege escalation
the reviewer was fixed to close.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.evals.spec import ASSERTION_KINDS, Assertion

# How long a single assertion command may run. Assertions are meant to be
# cheap and targeted -- one test file, one script -- and a spec that needs
# longer than this is asking the wrong question of a golden task.
COMMAND_TIMEOUT_S = 300


@dataclass
class AssertionContext:
    """Everything an assertion can see. Assembled once per task.

    `changed_paths` is repo-relative and posix-separated, which is what the
    globs in a spec are written against.
    """
    worktree: Path
    changed_paths: tuple[str, ...] = ()
    checks_pass: bool | None = None
    review_verdict: str | None = None
    iteration_count: int | None = None
    # Injected so the whole module is testable without Docker. The default is
    # the real sandbox; a test passes a fake.
    run_command: Any = None
    project: str = ""


@dataclass(frozen=True)
class AssertionResult:
    assertion: Assertion
    ok: bool
    detail: str
    # True when the assertion could not be evaluated at all -- the task never
    # reached a review, so `review_verdict` has nothing to compare. Counted as
    # a FAILURE, never as a pass: "we could not tell" and "it was fine" are
    # different answers and only one of them should let a suite go green.
    undetermined: bool = False


@dataclass
class TaskAssertionReport:
    results: list[AssertionResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def failures(self) -> list[AssertionResult]:
        return [r for r in self.results if not r.ok]


# --- glob matching ---------------------------------------------------------
#
# Written out rather than reached for, because fnmatch gets this wrong in the
# direction that matters here: its `*` compiles to `.*`, so `src/*.py` matches
# `src/deep/nested.py` and a `diff_excludes: ["tests/*"]` silently covers more
# than its author meant. Here `*` stops at a separator and `**` crosses them,
# which is what every spec author already assumes.

def _glob_to_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 3] == "**/":
                # `a/**/b` must match `a/b` as well as `a/x/b`, and must NOT
                # match `a/xb` -- so the separator is part of the optional
                # group rather than a literal that has to be there.
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")       # `a/**` covers `a/b` and `a/b/c`
                i += 2
                continue
            out.append("[^/]*")        # a single star stops at a separator
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(path: str, pattern: str) -> bool:
    return _glob_to_regex(pattern).match(path) is not None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise TypeError(f"expected a string or list of strings, got {value!r}")


# --- the evaluators --------------------------------------------------------
#
# One per kind in spec.ASSERTION_KINDS. Each returns (ok, detail); `detail` is
# what the report prints, so it says what was actually found rather than
# repeating the assertion back.

async def _checks_pass(a: Assertion, ctx: AssertionContext):
    if ctx.checks_pass is None:
        return None, "the task never reached the check gate"
    want = bool(a.value)
    return ctx.checks_pass is want, f"checks {'passed' if ctx.checks_pass else 'failed'}"


async def _review_verdict(a: Assertion, ctx: AssertionContext):
    if not ctx.review_verdict:
        return None, "the task never reached a review"
    want = str(a.value).upper()
    got = ctx.review_verdict.upper()
    return got == want, f"reviewer said {got}"


async def _max_iterations(a: Assertion, ctx: AssertionContext):
    if ctx.iteration_count is None:
        return None, "no iteration count recorded"
    # iteration_count counts REDOS, not passes (verify_and_ship only bumps it
    # in _loop_back), so 0 means it landed first time. Same convention as
    # agent/benchmarks.py, and the same trap.
    limit = int(a.value)
    return ctx.iteration_count <= limit, f"{ctx.iteration_count} redo(s), limit {limit}"


async def _file_matches(a: Assertion, ctx: AssertionContext):
    if not isinstance(a.value, dict) or "path" not in a.value:
        raise TypeError(f"file_matches needs {{path, pattern}}, got {a.value!r}")
    rel = str(a.value["path"])
    target = (ctx.worktree / rel).resolve()
    # The spec is operator-authored, but it is still a path joined onto a
    # directory -- keep it inside the worktree so a stray `../` reads nothing
    # it should not.
    if not str(target).startswith(str(ctx.worktree.resolve())):
        raise ValueError(f"file_matches path escapes the worktree: {rel!r}")
    if not target.is_file():
        return False, f"{rel} does not exist"
    pattern = a.value.get("pattern")
    if pattern is None:
        return True, f"{rel} exists"
    try:
        text = target.read_text(errors="replace")
    except OSError as e:
        return False, f"{rel} unreadable: {e}"
    hit = re.search(str(pattern), text) is not None
    return hit, f"{rel} {'matches' if hit else 'does not match'} /{pattern}/"


async def _file_absent(a: Assertion, ctx: AssertionContext):
    missing, present = [], []
    for rel in _as_list(a.value):
        (present if (ctx.worktree / rel).exists() else missing).append(rel)
    return not present, (f"still present: {', '.join(present)}" if present
                         else f"absent as required: {', '.join(missing)}")


async def _diff_touches(a: Assertion, ctx: AssertionContext):
    missed = [p for p in _as_list(a.value)
              if not any(path_matches(c, p) for c in ctx.changed_paths)]
    return not missed, (f"never changed: {', '.join(missed)}" if missed
                        else f"changed {len(ctx.changed_paths)} file(s) covering every pattern")


async def _diff_excludes(a: Assertion, ctx: AssertionContext):
    hits = [c for c in ctx.changed_paths
            if any(path_matches(c, p) for p in _as_list(a.value))]
    return not hits, (f"changed what it must not: {', '.join(sorted(hits))}" if hits
                      else "touched none of the excluded paths")


async def _command(a: Assertion, ctx: AssertionContext):
    """Run a command in the finished worktree, IN THE SANDBOX.

    The spec gives an argv list and run_shell_sandboxed takes a shell string,
    so the argv has to be QUOTED, not joined. The first end-to-end run is what
    made that concrete: a plain `" ".join` turned

        ["python3", "-c", "import sys; sys.path.insert(0,'.'); ..."]

    into `python3 -c import sys; sys.path.insert(0,'.')...`, which bash met
    with "syntax error near unexpected token" -- so the assertion failed for a
    reason that had nothing to do with the agent. A benchmark that fails tasks
    over its own quoting is worse than no benchmark, because the failures look
    like results.
    """
    argv = a.value if isinstance(a.value, list) else [a.value]
    if not argv or not all(isinstance(x, str | int | float) for x in argv):
        raise TypeError(f"command needs a non-empty argv list, got {a.value!r}")
    cmd = shlex.join(str(x) for x in argv)
    runner = ctx.run_command
    if runner is None:  # pragma: no cover - the real path needs Docker
        from agent.tools.sandbox import run_shell_sandboxed
        runner = run_shell_sandboxed
    result = await runner(cmd, str(ctx.worktree), timeout=COMMAND_TIMEOUT_S, network="none")
    code = result.get("exit_code")
    tail = (result.get("output") or "")[-600:]
    return bool(result.get("ok")), f"`{cmd}` exited {code}" + (f"\n{tail}" if not result.get("ok") else "")


_EVALUATORS = {
    "checks_pass": _checks_pass,
    "review_verdict": _review_verdict,
    "command": _command,
    "file_matches": _file_matches,
    "file_absent": _file_absent,
    "diff_touches": _diff_touches,
    "diff_excludes": _diff_excludes,
    "max_iterations": _max_iterations,
}

# The registry and the accepted-kinds list are the same fact written twice;
# tests/test_evals_spec.py asserts they agree, because a kind a spec accepts
# and this cannot score would blow up mid-run, after the money was spent.
assert set(_EVALUATORS) == set(ASSERTION_KINDS)


async def evaluate(assertions, ctx: AssertionContext) -> TaskAssertionReport:
    """Every assertion, even after one fails.

    Not short-circuited: the report is read to work out what a regression
    broke, and "the first of six failed" is a much weaker answer than "four of
    six failed, all of them about the diff".
    """
    report = TaskAssertionReport()
    for a in assertions:
        try:
            ok, detail = await _EVALUATORS[a.kind](a, ctx)
        except Exception as e:  # noqa: BLE001 -- a broken assertion is a failed
            # assertion, never a crashed run: eleven other tasks have already
            # been paid for by the time this one is scored.
            report.results.append(AssertionResult(a, False, f"could not evaluate: {e}"))
            continue
        if ok is None:
            report.results.append(AssertionResult(a, False, detail, undetermined=True))
        else:
            report.results.append(AssertionResult(a, bool(ok), detail))
    return report
