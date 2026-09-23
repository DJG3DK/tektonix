"""The golden-task specs, and the refusals that keep them honest.

A bad golden task does not fail loudly -- it passes, and the suite reports a
number that gets trusted because it came from a benchmark. So most of these
tests are about what the loader REFUSES, and the last group asserts the real
shipped suite is well-formed, which is the one that runs in CI.
"""
import textwrap

import pytest

from agent.evals import spec


def write(tmp_path, name, body):
    p = tmp_path / f"{name}.yaml"
    p.write_text(textwrap.dedent(body))
    return p


@pytest.fixture
def fixtures_dir(tmp_path):
    """A minimal valid fixture, so task tests can point at something real."""
    root = tmp_path / "fixtures" / "demo"
    (root / "files").mkdir(parents=True)
    (root / "files" / "package.json").write_text('{"scripts": {"test": "true"}}')
    (root / "fixture.yaml").write_text("description: a demo\nstack: node\n")
    return tmp_path / "fixtures"


def good_task(**over):
    body = {
        "id": "demo-task", "fixture": "demo", "category": "bug-fix",
        "goal": "do the thing", "assert": [{"checks_pass": True}],
    }
    body.update(over)
    import yaml
    return yaml.safe_dump(body)


# --- assertions ------------------------------------------------------------

def test_an_unknown_assertion_kind_is_refused_not_skipped(tmp_path):
    """The failure this prevents: a typo'd key silently drops a real check,
    the task passes with one fewer assertion than its author wrote, and
    nothing anywhere says so."""
    with pytest.raises(spec.SpecError, match="unknown assertion 'checks_passs'"):
        spec.parse_assertion({"checks_passs": True}, tmp_path / "x.yaml")


def test_an_assertion_naming_two_kinds_is_refused(tmp_path):
    with pytest.raises(spec.SpecError, match="exactly one kind"):
        spec.parse_assertion({"checks_pass": True, "diff_touches": ["a"]}, tmp_path / "x.yaml")


def test_why_and_guard_are_not_counted_as_kinds(tmp_path):
    a = spec.parse_assertion(
        {"diff_excludes": ["tests/**"], "why": "no weakening the suite", "guard": True},
        tmp_path / "x.yaml")
    assert a.kind == "diff_excludes" and a.guard is True
    assert a.describe() == "guard: no weakening the suite"


def test_guard_must_be_a_boolean(tmp_path):
    with pytest.raises(spec.SpecError, match="'guard' must be true or false"):
        spec.parse_assertion({"checks_pass": True, "guard": "yes"}, tmp_path / "x.yaml")


def test_describe_falls_back_to_the_assertion_itself(tmp_path):
    a = spec.parse_assertion({"checks_pass": True}, tmp_path / "x.yaml")
    assert "checks_pass" in a.describe()


# --- tasks -----------------------------------------------------------------

def test_a_task_with_no_assertions_is_refused(tmp_path, fixtures_dir):
    p = write(tmp_path, "demo-task", good_task(**{"assert": []}))
    with pytest.raises(spec.SpecError, match="non-empty list"):
        spec.load_task(p, fixtures_dir)


def test_a_task_of_nothing_but_guards_is_refused(tmp_path, fixtures_dir):
    """Every assertion already true means the task passes whatever the agent
    does -- the most expensive kind of nothing, because it looks like a pass."""
    p = write(tmp_path, "demo-task", good_task(**{
        "assert": [{"diff_excludes": ["x"], "guard": True},
                   {"file_absent": ["y"], "guard": True}]}))
    with pytest.raises(spec.SpecError, match="every assertion is a guard"):
        spec.load_task(p, fixtures_dir)


def test_one_goal_assertion_among_guards_is_enough(tmp_path, fixtures_dir):
    p = write(tmp_path, "demo-task", good_task(**{
        "assert": [{"diff_excludes": ["x"], "guard": True}, {"checks_pass": True}]}))
    assert len(spec.load_task(p, fixtures_dir).assertions) == 2


def test_the_id_must_match_the_filename(tmp_path, fixtures_dir):
    """They are one identity used two ways -- the filename is how a human
    finds a task, the id is how the report names it. Drift makes a report row
    nobody can grep for."""
    p = write(tmp_path, "demo-task", good_task(id="something-else"))
    with pytest.raises(spec.SpecError, match="must match the filename"):
        spec.load_task(p, fixtures_dir)


def test_a_category_outside_the_taxonomy_is_refused(tmp_path, fixtures_dir):
    p = write(tmp_path, "demo-task", good_task(category="refactoring"))
    with pytest.raises(spec.SpecError, match="is not one of"):
        spec.load_task(p, fixtures_dir)


@pytest.mark.parametrize("budget", [0, -1, "two", True])
def test_a_nonsense_budget_is_refused(tmp_path, fixtures_dir, budget):
    p = write(tmp_path, "demo-task", good_task(budget_usd=budget))
    with pytest.raises(spec.SpecError, match="budget_usd"):
        spec.load_task(p, fixtures_dir)


def test_a_missing_fixture_is_caught_at_load_not_mid_run(tmp_path, fixtures_dir):
    """Twenty minutes and nine paid-for tasks into a run is the wrong moment
    to discover a typo in a fixture name."""
    p = write(tmp_path, "demo-task", good_task(fixture="nope"))
    with pytest.raises(spec.SpecError, match="no fixture 'nope'"):
        spec.load_task(p, fixtures_dir)


def test_malformed_yaml_names_the_file(tmp_path, fixtures_dir):
    p = tmp_path / "demo-task.yaml"
    p.write_text("id: [unclosed\n")
    with pytest.raises(spec.SpecError, match="not valid YAML"):
        spec.load_task(p, fixtures_dir)


# --- fixtures --------------------------------------------------------------

def test_a_fixture_with_no_files_is_refused(tmp_path):
    root = tmp_path / "fixtures" / "empty"
    (root / "files").mkdir(parents=True)
    (root / "fixture.yaml").write_text("description: d\nstack: node\n")
    with pytest.raises(spec.SpecError, match="is empty"):
        spec.load_fixture("empty", tmp_path / "fixtures")


def test_a_fixture_name_that_is_not_a_safe_directory_is_refused(tmp_path):
    for bad in ("../etc", "Has-Caps", "", "a" * 80):
        with pytest.raises(spec.SpecError, match="fixture name"):
            spec.load_fixture(bad, tmp_path / "fixtures")


# --- the suite -------------------------------------------------------------

def test_every_broken_task_is_reported_at_once(tmp_path, fixtures_dir):
    """Fixing a suite one error per run is how people stop running it."""
    write(tmp_path, "a-bad", good_task(id="a-bad", category="nope"))
    write(tmp_path, "b-bad", good_task(id="b-bad", fixture="missing"))
    with pytest.raises(spec.SpecError) as e:
        spec.load_suite(tmp_path, fixtures_dir)
    assert "a-bad" in str(e.value) and "b-bad" in str(e.value)


def test_only_selects_and_an_unknown_id_is_an_error(tmp_path, fixtures_dir):
    write(tmp_path, "one-task", good_task(id="one-task"))
    write(tmp_path, "two-task", good_task(id="two-task"))
    assert [t.id for t in spec.load_suite(tmp_path, fixtures_dir, only=["two-task"])] == ["two-task"]
    with pytest.raises(spec.SpecError, match="no such task"):
        spec.load_suite(tmp_path, fixtures_dir, only=["three-task"])


# --- the suite that actually ships -----------------------------------------

def test_the_shipped_suite_loads():
    tasks = spec.load_suite()
    assert len(tasks) >= 10, "under ten tasks is a sample the panel itself calls too thin"


def test_the_shipped_suite_covers_the_categories_the_agent_routes_on():
    """A suite that skips ui-styling measures five sixths of the agent -- that
    category lands on a different coder seat entirely."""
    covered = {t.category for t in spec.load_suite()}
    for needed in ("bug-fix", "feature", "ui-styling"):
        assert needed in covered, f"no golden task exercises {needed}"


def test_every_shipped_task_has_at_least_one_goal_assertion():
    for t in spec.load_suite():
        assert any(not a.guard for a in t.assertions), f"{t.id} is all guards"


def test_every_accepted_assertion_kind_can_actually_be_scored():
    """A kind the spec accepts and the harness cannot evaluate would blow up
    mid-run, after the money was spent."""
    from agent.evals.assertions import _EVALUATORS
    assert set(_EVALUATORS) == set(spec.ASSERTION_KINDS)
