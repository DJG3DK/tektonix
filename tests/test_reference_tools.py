"""Reading another project from inside a build task.

"Use the checkout flow from the other project as a template" is an ordinary
request, and it used to reach a build task only as prose in a plan: the
planner could read both projects, the builder could read nothing but its own.
Typed straight into the task composer it could not be done at all.

These tools are the narrow version. What the tests below pin is the narrow
part -- what they may read, what they refuse, and that the refusals say
something a model can act on instead of failing silently.
"""

import pytest

from agent.tools import reference_tools as rt


@pytest.fixture
def projects(monkeypatch, tmp_path):
    """Three configured projects, each with a worktree holding one file."""
    made = {}
    for name in ("shop", "blog", "secret"):
        root = tmp_path / name
        (root / "src").mkdir(parents=True)
        (root / "src" / "checkout.ts").write_text(
            f"// {name}\nexport function checkout() {{ return {name!r}; }}\n")
        made[name] = {"sandbox": str(root), "live": f"/live/{name}"}
    monkeypatch.setattr(rt, "PROJECTS", made, raising=False)
    return tmp_path


def _by_name(tools):
    return {t.name: t for t in tools}


def test_no_other_projects_means_no_tools(projects):
    """A one-project deployment is not offered four tools that can only
    refuse -- and the model is not invited to try."""
    assert rt.make_reference_tools("shop", []) == []
    assert rt.make_reference_tools("shop", None) == []
    assert rt.make_reference_tools("shop", ["shop"]) == [], "its own repo is not a reference"


def test_the_readable_projects_are_named_in_every_description(projects):
    tools = rt.make_reference_tools("shop", ["shop", "blog", "secret"])
    assert len(tools) == 4
    for t in tools:
        assert "blog, secret" in t.description, f"{t.name} does not say what it can reach"


def test_it_reads_another_project(projects):
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    out = tools["read_project_file"].invoke({"repo": "blog", "path": "src/checkout.ts"})
    assert "// blog" in out


def test_it_searches_another_project(projects):
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    out = tools["search_project"].invoke({"repo": "blog", "pattern": "checkout"})
    assert "checkout.ts" in out


def test_it_lists_and_finds_in_another_project(projects):
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    assert "src/" in tools["list_project_dir"].invoke({"repo": "blog", "path": "."})
    assert "checkout.ts" in tools["find_files"].invoke({"repo": "blog", "glob": "**/*.ts"})


# --- what it refuses ------------------------------------------------------

def test_a_project_the_task_may_not_read_is_refused(projects):
    """The whole point of carrying the creator's access onto the task."""
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    for name, args in (
        ("read_project_file", {"repo": "secret", "path": "src/checkout.ts"}),
        ("search_project", {"repo": "secret", "pattern": "checkout"}),
        ("list_project_dir", {"repo": "secret", "path": "."}),
        ("find_files", {"repo": "secret", "glob": "**/*"}),
    ):
        out = tools[name].invoke(args)
        assert out.startswith("ERROR:") and "may not read" in out, name
        assert "secret" not in out.replace("'secret'", ""), \
            f"{name} leaked the contents of a project it refused"


def test_the_tasks_own_repo_is_refused_and_says_what_to_use(projects):
    """Two ways to read the same file with different path semantics is how a
    model ends up editing through the wrong one."""
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    out = tools["read_project_file"].invoke({"repo": "shop", "path": "src/checkout.ts"})
    assert out.startswith("ERROR:")
    assert "this task is working on" in out and "read, bash" in out


def test_an_unknown_project_names_the_ones_that_exist(projects):
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    out = tools["read_project_file"].invoke({"repo": "nope", "path": "x"})
    assert "unknown project" in out and "blog" in out


@pytest.mark.parametrize("path", ["../secret/src/checkout.ts", "/etc/passwd",
                                  "src/../../secret/src/checkout.ts"])
def test_a_path_cannot_walk_out_of_the_project_it_named(projects, path):
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    out = tools["read_project_file"].invoke({"repo": "blog", "path": path})
    assert out.startswith("ERROR:"), f"{path} was not refused"
    assert "// secret" not in out and "root:" not in out


def test_a_missing_file_is_reported_not_raised(projects):
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    out = tools["read_project_file"].invoke({"repo": "blog", "path": "src/nope.ts"})
    assert "does not exist" in out


def test_a_project_with_no_workspace_is_refused(projects, monkeypatch):
    monkeypatch.setitem(rt.PROJECTS, "blog", {"live": "/live/blog"})
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    out = tools["list_project_dir"].invoke({"repo": "blog", "path": "."})
    assert "no workspace" in out


# --- paging ---------------------------------------------------------------

def test_a_big_file_pages_instead_of_returning_the_same_text_forever(projects, tmp_path):
    # Past the 40k inline cap, which is the only way to reach the truncation
    # path -- 4,000 bare "line N" lines fit under it.
    big = "\n".join(f"line {i} " + "x" * 24 for i in range(1, 4001))
    (tmp_path / "blog" / "big.txt").write_text(big)
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))

    first = tools["read_project_file"].invoke({"repo": "blog", "path": "big.txt"})
    assert "TRUNCATED" in first and "offset=" in first, \
        "a truncated read must say how to get the rest"

    paged = tools["read_project_file"].invoke(
        {"repo": "blog", "path": "big.txt", "offset": 3000, "limit": 600})
    assert "line 3000 " in paged and "line 3599 " in paged
    assert "line 2999 " not in paged
    assert "of 4000" in paged


def test_a_window_past_the_end_says_so_rather_than_returning_nothing(projects, tmp_path):
    (tmp_path / "blog" / "small.txt").write_text("one\ntwo\n")
    tools = _by_name(rt.make_reference_tools("shop", ["shop", "blog"]))
    out = tools["read_project_file"].invoke(
        {"repo": "blog", "path": "small.txt", "offset": 500, "limit": 10})
    assert "no lines at offset 500" in out
