"""A build task's files are supposed to be missing.

Live on 2026-09-14, task 3ee0d030. The goal opened "Add a new strategy
`multiTrader`" and gave a table of three files to create. The agent spent
1h49m, $6.98 and 754 tool calls, wrote nothing at all -- the worktree was still
clean, no branch, no untracked files -- and stopped to ask the operator about
"a fundamental problem I need to resolve with you rather than guess at". There
was no problem: the worktree sat at the same commit as live main with every
referenced file present. The only things missing were the files it had been
asked to create.

Seven subagent delegations made it worse. Each starts with a fresh context, so
an investigator told to look at `multiTrader` searches, finds nothing, and
correctly reports absence -- and the coordinator hears "it does not exist" over
and over and reads a blocker.

So absence is computed from the goal and stated as a fact in every prompt,
rather than left for the model to infer from prose it has already read.
"""

from __future__ import annotations

import pytest
from urllib.parse import urlparse

from agent import new_files

GOAL = """# Build Plan — `multiTrader` strategy (4-engine regime-routed entry system)

Add a new strategy `multiTrader` to webapp that unifies four entry engines.

| File | Role |
|---|---|
| `src/strategies/multiTrader.js` | Live glue |
| `src/strategies/modules/mtCore.js` | Pure math, no I/O |
| `src/core/mtBacktester.js` | Job wrapper |

It reuses bot.js openPosition and mirrors `src/strategies/scout.js`'s latch.
Background: https://example.com/docs/guide.js
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src" / "strategies").mkdir(parents=True)
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "strategies" / "scout.js").write_text("// exists\n")
    return str(tmp_path)


def test_it_finds_the_paths_the_plan_names(repo):
    found = new_files.declared_paths(GOAL)
    assert "src/strategies/multiTrader.js" in found
    assert "src/strategies/modules/mtCore.js" in found
    assert "src/core/mtBacktester.js" in found


def test_a_url_is_not_a_repo_path():
    """`https://example.com/docs/guide.js` ends in .js and has slashes."""
    found = new_files.declared_paths(GOAL)
    assert "docs/guide.js" not in found
    # Assert the shape, not a substring. `"example.com" in p` matches a path
    # that merely CONTAINS the host anywhere -- including one that only looks
    # like a URL because a directory is named after a domain (CodeQL
    # py/incomplete-url-substring-sanitization). What actually matters is that
    # nothing which came out of a URL survived as a repo path.
    for p in found:
        assert "://" not in p, f"{p!r} is a URL, not a path in the repo"
        assert not p.startswith("//"), f"{p!r} is protocol-relative"
        assert urlparse("//" + p).hostname != "example.com", f"{p!r} kept a host"


def test_only_the_missing_ones_are_reported(repo):
    """scout.js exists and is referenced as prior art. Listing it would be
    both wrong and the kind of noise that gets a prompt block ignored."""
    missing = new_files.absent_paths(repo, new_files.declared_paths(GOAL))
    assert "src/strategies/scout.js" not in missing
    assert len(missing) == 3


def test_the_guidance_names_the_files_and_says_absence_is_expected(repo):
    g = new_files.guidance(repo, GOAL)
    assert "src/strategies/multiTrader.js" in g
    assert "EXPECTED starting state" in g
    # the two behaviours that actually cost 1h49m
    assert "never a reason to stop and ask the operator" in g
    assert "do not delegate a subagent to look for them" in g


def test_nothing_is_said_when_every_named_path_exists(repo, tmp_path):
    """A prompt block that appears on every task stops being read."""
    goal = "Fix the latch in `src/strategies/scout.js`."
    assert new_files.guidance(repo, goal) == ""


def test_a_goal_with_no_paths_is_silent(repo):
    assert new_files.guidance(repo, "Make the dashboard faster") == ""
    assert new_files.guidance(repo, "") == ""


def test_an_unreadable_repo_claims_nothing(tmp_path):
    """Not being able to look is different from having looked and found
    nothing, and only one of those is worth telling the model."""
    assert new_files.absent_paths(str(tmp_path / "nope"), ["a/b.js"]) == []
    assert new_files.guidance("", GOAL) == ""


def test_the_list_is_capped(repo):
    many = "\n".join(f"- `src/gen/file{i}.js`" for i in range(60))
    g = new_files.guidance(repo, many)
    assert g.count("  - src/gen/") <= new_files.MAX_LISTED
    assert "and 35 more" in g


def test_every_prompt_carries_it():
    """Coordinator AND subagents: the subagents are what went looking."""
    import inspect

    from agent import deep_agent

    src = inspect.getsource(deep_agent.build_deep_agent)
    assert "absent_files = new_files.guidance(repo_root, goal)" in src
    for prompt in ("INVESTIGATOR_SYSTEM_PROMPT + absent_files",
                   "TEST_WRITER_SYSTEM_PROMPT + absent_files",
                   "_FILESYSTEM_GUIDANCE + absent_files"):
        assert prompt in src, prompt
    assert ") + absent_files," in src, "the coordinator's own prompt"


def test_the_work_node_passes_the_goal():
    """build_deep_agent never saw the goal before this; the block is empty
    without it and the whole thing silently does nothing."""
    import inspect

    from agent.nodes import work

    assert 'goal=state.get("goal") or ""' in inspect.getsource(work.work_node)
