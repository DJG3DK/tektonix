"""What a golden task and a fixture are, and refusing the ones that are wrong.

Validation here is strict and loud on purpose. Every other kind of config in
this system fails visibly when it is malformed -- a bad projects.json stops a
review, a bad model pin shows up in the dashboard. A bad golden task fails
INVISIBLY: an assertion that silently evaluates to "nothing to check" makes a
task pass, the suite goes green, and the number it produces is trusted
precisely because it came from a benchmark.

So an unknown assertion key is an error rather than a skip, an empty assertion
list is an error rather than a vacuous pass, and every path a spec names is
resolved and checked at load time -- before any money is spent -- instead of
at the point of use, which on a twelve-task run is twenty minutes later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent import paths

EVALS_DIR = paths.REPO_ROOT / "evals"
TASKS_DIR = EVALS_DIR / "tasks"
FIXTURES_DIR = EVALS_DIR / "fixtures"

# The categories agent/classify.py already knows. A golden task declares one so
# a run can be read per category ("it got worse at bug-fixes"), and declaring
# one outside the taxonomy would produce a row that matches nothing on the
# Analytics page.
CATEGORIES = ("bug-fix", "feature", "ui-styling", "performance", "investigation",
              # 2026-09-23, when the suite grew from 12 tasks to 30: work a real
              # project asks for that none of the above names.
              "security", "refactor", "tests", "other")

# Same shape as a project name, because that is what a fixture becomes: a
# directory basename and a key in the eval's own projects.json.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# Every assertion kind the harness can evaluate. agent/evals/assertions.py
# holds one evaluator per entry; the two lists are checked against each other
# by a test, because a kind named here and unimplemented would accept a spec
# the runner then cannot score.
ASSERTION_KINDS = (
    "checks_pass",     # the gate's own check suite went green
    "review_verdict",  # what the real reviewer said
    "command",         # a command run IN THE SANDBOX against the finished tree
    "file_matches",    # a file exists and matches a regex
    "file_absent",     # a file is not there (deleting the test is not a fix)
    "diff_touches",    # the diff changed every one of these globs
    "diff_excludes",   # the diff changed none of these globs
    "max_iterations",  # it got there without N redos
)


class SpecError(ValueError):
    """A spec that must not be run. Carries the file it came from, because a
    twelve-task suite reports these in a batch and 'missing goal' alone names
    nothing."""


@dataclass(frozen=True)
class Assertion:
    kind: str
    value: Any
    # Free text from the spec, shown in the report next to pass/fail. A
    # failing assertion that can only say `diff_excludes: ['tests/**']` makes
    # the reader reconstruct the intent; `why` lets the spec state it.
    why: str = ""
    # A guard must STAY true; everything else must BECOME true.
    #
    # The distinction earns its keep in --verify, which refuses a suite whose
    # assertions already pass on the pristine fixture -- an assertion that is
    # true before the agent runs tests nothing, and a task made entirely of
    # them reports success however badly the agent behaves. But "the original
    # tests are still there" and "the diff did not touch package.json" are
    # true at the start BY DESIGN: they are there to catch the agent making
    # them false. Without this flag the vacuity check has to be switched off,
    # and then it protects nothing.
    guard: bool = False

    def describe(self) -> str:
        return ("guard: " if self.guard else "") + (self.why or f"{self.kind}: {self.value!r}")


@dataclass(frozen=True)
class FixtureSpec:
    name: str
    root: Path
    description: str
    stack: str
    # Goes into the eval's projects.json under this fixture's entry. The
    # reviewer reads its check commands from there, exactly as it does for a
    # real project -- the fixture is not special-cased anywhere downstream.
    review: dict = field(default_factory=dict)

    @property
    def files_dir(self) -> Path:
        return self.root / "files"


@dataclass(frozen=True)
class TaskSpec:
    id: str
    fixture: str
    category: str
    goal: str
    budget_usd: float
    assertions: tuple[Assertion, ...]
    path: Path
    # A task the suite carries but does not run by default: kept for the
    # record (a reproduction of a bug that is now fixed) without paying for
    # it on every run.
    skip: str = ""


def _require(mapping: dict, key: str, where: Path, typ: type | tuple[type, ...]) -> Any:
    if key not in mapping:
        raise SpecError(f"{where}: missing required key {key!r}")
    value = mapping[key]
    if not isinstance(value, typ) or (typ is str and not value.strip()):
        name = typ.__name__ if isinstance(typ, type) else "/".join(t.__name__ for t in typ)
        raise SpecError(f"{where}: {key!r} must be a non-empty {name}, got {value!r}")
    return value


def parse_assertion(raw: Any, where: Path) -> Assertion:
    """One assertion, or a refusal naming what was wrong with it.

    A bare mapping of one key is the whole grammar: `{checks_pass: true}`.
    `why` is the one reserved second key. Anything else in the mapping is an
    error rather than an ignored extra -- a typo'd key that is silently
    dropped turns a real check into no check at all, and the suite still goes
    green.
    """
    if not isinstance(raw, dict) or not raw:
        raise SpecError(f"{where}: each assertion must be a non-empty mapping, got {raw!r}")
    why = raw.get("why", "")
    if not isinstance(why, str):
        raise SpecError(f"{where}: 'why' must be a string, got {why!r}")
    guard = raw.get("guard", False)
    if not isinstance(guard, bool):
        raise SpecError(f"{where}: 'guard' must be true or false, got {guard!r}")
    keys = [k for k in raw if k not in ("why", "guard")]
    if len(keys) != 1:
        raise SpecError(
            f"{where}: an assertion names exactly one kind (plus optional 'why'/'guard'), "
            f"got {keys!r}")
    kind = keys[0]
    if kind not in ASSERTION_KINDS:
        raise SpecError(
            f"{where}: unknown assertion {kind!r}. Known kinds: {', '.join(ASSERTION_KINDS)}")
    return Assertion(kind=kind, value=raw[kind], why=why, guard=guard)


def load_fixture(name: str, fixtures_dir: Path | None = None) -> FixtureSpec:
    base = fixtures_dir or FIXTURES_DIR
    if not _NAME_RE.match(name or ""):
        raise SpecError(f"fixture name {name!r} must be lowercase letters, digits and hyphens")
    root = base / name
    meta_path = root / "fixture.yaml"
    if not meta_path.is_file():
        raise SpecError(f"no fixture {name!r}: expected {meta_path}")
    try:
        meta = yaml.safe_load(meta_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SpecError(f"{meta_path}: not valid YAML: {e}") from None
    if not isinstance(meta, dict):
        raise SpecError(f"{meta_path}: expected a mapping at the top level")
    spec = FixtureSpec(
        name=name,
        root=root,
        description=_require(meta, "description", meta_path, str),
        stack=_require(meta, "stack", meta_path, str),
        review=meta.get("review") or {},
    )
    if not spec.files_dir.is_dir():
        raise SpecError(f"{root}: a fixture needs a files/ directory holding the repo contents")
    if not any(spec.files_dir.rglob("*")):
        raise SpecError(f"{spec.files_dir}: is empty -- a fixture with no files is not a repo")
    if not isinstance(spec.review, dict):
        raise SpecError(f"{meta_path}: 'review' must be a mapping, got {spec.review!r}")
    return spec


def load_task(path: Path, fixtures_dir: Path | None = None) -> TaskSpec:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SpecError(f"{path}: not valid YAML: {e}") from None
    except OSError as e:
        raise SpecError(f"{path}: unreadable: {e}") from None
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")

    task_id = _require(raw, "id", path, str)
    if not _NAME_RE.match(task_id):
        raise SpecError(f"{path}: id {task_id!r} must be lowercase letters, digits and hyphens")
    if task_id != path.stem:
        # They are the same identity used two ways -- the filename is how a
        # human finds the task, the id is how the report names it. Letting
        # them drift means a report row nobody can grep for.
        raise SpecError(f"{path}: id {task_id!r} must match the filename ({path.stem!r})")

    category = _require(raw, "category", path, str)
    if category not in CATEGORIES:
        raise SpecError(f"{path}: category {category!r} is not one of {', '.join(CATEGORIES)}")

    budget = raw.get("budget_usd", 2.0)
    if not isinstance(budget, int | float) or isinstance(budget, bool) or budget <= 0:
        raise SpecError(f"{path}: budget_usd must be a positive number, got {budget!r}")

    raw_assertions = raw.get("assert")
    if not isinstance(raw_assertions, list) or not raw_assertions:
        # Not a warning. A task with no assertions is scored purely on
        # "did it finish", which is the pass bar this suite exists to beat.
        raise SpecError(f"{path}: 'assert' must be a non-empty list -- a task with no "
                        f"assertions cannot tell a real fix from a plausible one")
    parsed = tuple(parse_assertion(a, path) for a in raw_assertions)
    if all(a.guard for a in parsed):
        # Every assertion is one that was already true. Nothing here can
        # become true, so the task passes whatever the agent does.
        raise SpecError(f"{path}: every assertion is a guard -- nothing here tests that the "
                        f"agent did the thing, only that it did not break something else")

    fixture_name = _require(raw, "fixture", path, str)
    # Loaded, not merely named: a task pointing at a fixture that does not
    # exist must fail now, not twenty minutes and nine tasks into a run.
    try:
        load_fixture(fixture_name, fixtures_dir)
    except SpecError as e:
        # Re-raised with the TASK's path in front. load_fixture reports the
        # fixture it could not find, which is the right message on its own and
        # the wrong one inside a twelve-task batch: the reader needs to know
        # which spec to go and edit, and "no fixture 'demo'" names no file.
        raise SpecError(f"{path}: {e}") from None

    skip = raw.get("skip", "")
    if not isinstance(skip, str):
        raise SpecError(f"{path}: 'skip' must be a string reason, got {skip!r}")

    return TaskSpec(
        id=task_id,
        fixture=fixture_name,
        category=category,
        goal=_require(raw, "goal", path, str).strip(),
        budget_usd=float(budget),
        assertions=parsed,
        path=path,
        skip=skip,
    )


def load_suite(tasks_dir: Path | None = None, fixtures_dir: Path | None = None,
               only: list[str] | None = None) -> list[TaskSpec]:
    """Every golden task, in a stable order, or a refusal listing ALL the
    broken ones.

    All of them, not the first: fixing a suite one error per run is the sort
    of thing that makes people stop running it.
    """
    base = tasks_dir or TASKS_DIR
    if not base.is_dir():
        raise SpecError(f"no tasks directory at {base}")
    files = sorted(p for p in base.glob("*.yaml"))
    if not files:
        raise SpecError(f"{base}: no .yaml task files")

    tasks, errors = [], []
    for path in files:
        try:
            tasks.append(load_task(path, fixtures_dir))
        except SpecError as e:
            errors.append(str(e))
    if errors:
        raise SpecError("the eval suite has broken task specs:\n  - " + "\n  - ".join(errors))

    if only:
        wanted = set(only)
        unknown = wanted - {t.id for t in tasks}
        if unknown:
            raise SpecError(f"no such task(s): {', '.join(sorted(unknown))}")
        tasks = [t for t in tasks if t.id in wanted]
    return tasks
