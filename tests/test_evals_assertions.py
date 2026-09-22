"""The predicates a golden task is scored on.

Every one of these decides whether a benchmark reports success, so they are
tested against a real directory rather than a mock filesystem -- the whole
value of an assertion is that it looks at what is actually there.
"""
from pathlib import Path

import pytest

from agent.evals.assertions import (
    AssertionContext,
    evaluate,
    path_matches,
)
from agent.evals.spec import Assertion


def A(kind, value, guard=False):
    return Assertion(kind=kind, value=value, guard=guard)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "report.py").write_text("import heapq\n\ndef top_n(rows, n):\n    ...\n")
    (tmp_path / "tests" / "test_report.py").write_text("def test_total():\n    pass\n")
    return tmp_path


# --- glob semantics --------------------------------------------------------
#
# These decide what `diff_excludes: ["tests/**"]` covers, so getting them
# wrong silently widens or narrows an assertion without failing anything.

@pytest.mark.parametrize("path,pattern,expected", [
    ("tests/foo.py", "tests/**", True),
    ("tests/deep/foo.py", "tests/**", True),
    ("src/foo.py", "tests/**", False),
    # A single star stops at a separator. fnmatch gets this wrong -- its `*`
    # compiles to `.*` -- which would make this pattern cover the whole tree.
    ("src/a.py", "src/*.py", True),
    ("src/deep/a.py", "src/*.py", False),
    # `a/**/b` covers zero intermediate directories as well as many...
    ("a/b", "a/**/b", True),
    ("a/x/y/b", "a/**/b", True),
    # ...but must not swallow the separator itself.
    ("a/xb", "a/**/b", False),
    ("anything", "**", True),
    ("pkg/test_a.py", "**/test_*.py", True),
    ("test_a.py", "**/test_*.py", True),
    ("src/report.py", "src/report.py", True),
])
def test_glob_matching(path, pattern, expected):
    assert path_matches(path, pattern) is expected


# --- diff assertions -------------------------------------------------------

@pytest.mark.asyncio
async def test_diff_touches_requires_every_pattern(tree):
    ctx = AssertionContext(worktree=tree, changed_paths=("src/report.py",))
    r = await evaluate([A("diff_touches", ["src/report.py", "tests/**"])], ctx)
    assert not r.passed
    assert "tests/**" in r.results[0].detail


@pytest.mark.asyncio
async def test_diff_excludes_catches_the_weakened_test(tree):
    """The failure the whole suite exists for: a green gate cannot see that
    the fix was to edit the test."""
    ctx = AssertionContext(worktree=tree, changed_paths=("src/report.py", "tests/test_report.py"))
    r = await evaluate([A("diff_excludes", ["tests/**"])], ctx)
    assert not r.passed
    assert "tests/test_report.py" in r.results[0].detail


@pytest.mark.asyncio
async def test_diff_excludes_passes_when_nothing_matched(tree):
    ctx = AssertionContext(worktree=tree, changed_paths=("src/report.py",))
    assert (await evaluate([A("diff_excludes", ["tests/**"])], ctx)).passed


# --- file assertions -------------------------------------------------------

@pytest.mark.asyncio
async def test_file_matches_reads_the_file(tree):
    ctx = AssertionContext(worktree=tree)
    ok = await evaluate([A("file_matches", {"path": "src/report.py", "pattern": "heapq"})], ctx)
    bad = await evaluate([A("file_matches", {"path": "src/report.py", "pattern": "bisect"})], ctx)
    assert ok.passed and not bad.passed


@pytest.mark.asyncio
async def test_file_matches_with_no_pattern_is_an_existence_check(tree):
    ctx = AssertionContext(worktree=tree)
    assert (await evaluate([A("file_matches", {"path": "src/report.py"})], ctx)).passed
    assert not (await evaluate([A("file_matches", {"path": "src/nope.py"})], ctx)).passed


@pytest.mark.asyncio
async def test_file_matches_refuses_to_read_outside_the_worktree(tree):
    """The spec is operator-authored, but it is still a path joined onto a
    directory, and an assertion that can read /etc is a footgun with no use."""
    ctx = AssertionContext(worktree=tree)
    r = await evaluate([A("file_matches", {"path": "../../../etc/passwd"})], ctx)
    assert not r.passed
    assert "escapes the worktree" in r.results[0].detail


@pytest.mark.asyncio
async def test_file_absent(tree):
    ctx = AssertionContext(worktree=tree)
    assert (await evaluate([A("file_absent", ["src/gone.py"])], ctx)).passed
    r = await evaluate([A("file_absent", ["src/report.py"])], ctx)
    assert not r.passed and "still present" in r.results[0].detail


# --- run-derived assertions ------------------------------------------------

@pytest.mark.asyncio
async def test_checks_pass_and_review_verdict(tree):
    ctx = AssertionContext(worktree=tree, checks_pass=True, review_verdict="READY")
    assert (await evaluate([A("checks_pass", True), A("review_verdict", "READY")], ctx)).passed
    assert (await evaluate([A("review_verdict", "ready")], ctx)).passed  # case-insensitive


@pytest.mark.asyncio
async def test_an_unevaluable_assertion_fails_rather_than_passes(tree):
    """'We could not tell' and 'it was fine' are different answers, and only
    one of them should let a suite go green."""
    ctx = AssertionContext(worktree=tree, checks_pass=None, review_verdict=None)
    r = await evaluate([A("checks_pass", True), A("review_verdict", "READY")], ctx)
    assert not r.passed
    assert all(x.undetermined for x in r.results)


@pytest.mark.asyncio
async def test_max_iterations_counts_redos_not_passes(tree):
    """iteration_count is bumped only by _loop_back, so 0 is 'landed first
    time'. Reading it as a pass count would let one redo through."""
    assert (await evaluate([A("max_iterations", 0)],
                           AssertionContext(worktree=tree, iteration_count=0))).passed
    assert not (await evaluate([A("max_iterations", 0)],
                               AssertionContext(worktree=tree, iteration_count=1))).passed


# --- the command assertion -------------------------------------------------

@pytest.mark.asyncio
async def test_command_goes_through_the_injected_sandbox_runner(tree):
    """It runs against a worktree the agent just wrote. Shelling out on the
    host would reintroduce, for measurement, the escalation the reviewer was
    fixed to close."""
    seen = {}

    async def fake(cmd, cwd, timeout=None, network=None):
        seen.update(cmd=cmd, cwd=cwd, network=network)
        return {"ok": True, "exit_code": 0, "output": ""}

    ctx = AssertionContext(worktree=tree, run_command=fake)
    assert (await evaluate([A("command", ["python3", "-c", "pass"])], ctx)).passed
    assert seen["cmd"] == "python3 -c pass"
    assert seen["cwd"] == str(tree)
    assert seen["network"] == "none", "an assertion must not reach the network"


@pytest.mark.asyncio
async def test_a_failing_command_carries_its_output(tree):
    async def fake(cmd, cwd, timeout=None, network=None):
        return {"ok": False, "exit_code": 2, "output": "AssertionError: boom"}

    r = await evaluate([A("command", ["false"])], AssertionContext(worktree=tree, run_command=fake))
    assert not r.passed
    assert "exited 2" in r.results[0].detail and "boom" in r.results[0].detail


# --- the report ------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_assertion_is_evaluated_even_after_one_fails(tree):
    """'Four of six failed, all about the diff' says what broke; 'the first of
    six failed' sends the reader back to run it again."""
    ctx = AssertionContext(worktree=tree, changed_paths=("tests/test_report.py",))
    r = await evaluate([
        A("diff_excludes", ["tests/**"]),
        A("file_matches", {"path": "src/report.py", "pattern": "heapq"}),
        A("file_absent", ["src/report.py"]),
    ], ctx)
    assert len(r.results) == 3
    assert [x.ok for x in r.results] == [False, True, False]


@pytest.mark.asyncio
async def test_a_broken_assertion_fails_the_task_rather_than_the_run(tree):
    """Eleven other tasks have already been paid for by the time this one is
    scored -- a malformed value must not take the run down with it."""
    r = await evaluate([A("file_matches", "not-a-mapping")], AssertionContext(worktree=tree))
    assert not r.passed
    assert "could not evaluate" in r.results[0].detail


@pytest.mark.asyncio
async def test_an_empty_assertion_list_does_not_count_as_passed(tree):
    assert not (await evaluate([], AssertionContext(worktree=tree))).passed


@pytest.mark.asyncio
async def test_a_command_with_quoting_survives_the_trip_to_the_shell(tree):
    """The first end-to-end run failed a task over this and not over anything
    the agent did: a plain join turned `-c "import sys; x(0,'.')"` into shell
    that bash refused to parse."""
    seen = {}

    async def fake(cmd, cwd, timeout=None, network=None):
        seen["cmd"] = cmd
        # Prove it round-trips: what the shell would hand back as argv.
        import shlex
        seen["argv"] = shlex.split(cmd)
        return {"ok": True, "exit_code": 0, "output": ""}

    argv = ["python3", "-c", "import sys; sys.path.insert(0,'.'); assert f([]) == 0.0"]
    await evaluate([A("command", argv)], AssertionContext(worktree=tree, run_command=fake))
    assert seen["argv"] == argv
