"""Covers the build agent's read/write/edit tools on filesystem errors that
are NOT FileNotFoundError.

These used to escape the tool entirely. langgraph's ToolNode re-raises any
exception that isn't a ToolInvocationError, so an escaping OSError doesn't
fail one tool call -- it tears down the whole graph run mid-task. The
concrete trigger was a git worktree's `.git`, which is a one-line pointer
FILE: anything under `.git/` raises NotADirectoryError [Errno 20], and every
project sandbox here is a worktree.
"""

from agent.tools.agent_tools import make_agent_tools


def _tools(tmp_path):
    tools, _ = make_agent_tools(str(tmp_path))
    return {t.name: t for t in tools}


def _worktree_git_pointer(tmp_path) -> None:
    (tmp_path / ".git").write_text("gitdir: /home/my-service/.git/worktrees/my-service\n", encoding="utf-8")


async def test_read_through_a_worktree_git_pointer_returns_an_error(tmp_path):
    _worktree_git_pointer(tmp_path)
    result = await _tools(tmp_path)["read"].ainvoke({"path": ".git/HEAD"})
    assert result.startswith("ERROR:")
    assert "worktree" in result
    # files.py deliberately keeps the host sandbox root out of model-visible text.
    assert str(tmp_path) not in result


async def test_read_on_a_directory_returns_an_error(tmp_path):
    (tmp_path / "src").mkdir()
    result = await _tools(tmp_path)["read"].ainvoke({"path": "src"})
    assert result.startswith("ERROR:")
    assert "directory" in result


def test_write_through_a_worktree_git_pointer_returns_an_error(tmp_path):
    _worktree_git_pointer(tmp_path)
    result = _tools(tmp_path)["write"].invoke({"path": ".git/hooks/pre-commit", "content": "x"})
    assert result.startswith("ERROR:")
    assert str(tmp_path) not in result


def test_write_onto_an_existing_directory_returns_an_error(tmp_path):
    (tmp_path / "src").mkdir()
    result = _tools(tmp_path)["write"].invoke({"path": "src", "content": "x"})
    assert result.startswith("ERROR:")


def test_edit_through_a_worktree_git_pointer_returns_an_error(tmp_path):
    _worktree_git_pointer(tmp_path)
    result = _tools(tmp_path)["edit"].invoke(
        {"path": ".git/HEAD", "old_string": "a", "new_string": "b"}
    )
    assert result.startswith("ERROR:")
    assert str(tmp_path) not in result


async def test_an_unexpected_read_failure_returns_a_string_instead_of_raising(tmp_path, monkeypatch):
    def _explode(*_a, **_kw):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr("agent.tools.agent_tools.read_file", _explode)
    result = await _tools(tmp_path)["read"].ainvoke({"path": "a.txt"})
    assert result.startswith("ERROR:")
    assert "something nobody predicted" in result


# ---------------------------------------------------------------------------
# own-space redirect -- wrong tool, right correction (2026-10-02, live: a coder
# called `read` on /skills/cta-architecture/SKILL.md and got the generic
# "paths must be RELATIVE" escape error, which names no way forward).
# ---------------------------------------------------------------------------


async def test_an_own_space_path_names_the_matching_builtin_tool(tmp_path):
    tools = _tools(tmp_path)
    for path in ("/skills/cta-architecture/SKILL.md", "/memories/AGENTS.md", "/org-memory/AGENTS.md",
                 "/episodes/x.md"):
        result = await tools["read"].ainvoke({"path": path})
        assert result.startswith("ERROR:") and "YOUR OWN file space" in result and "read_file" in result
        assert "RELATIVE" not in result
        assert str(tmp_path) not in result
    # `file_path` spelling too
    result = await tools["read"].ainvoke({"file_path": "/skills/codebase-map/SKILL.md"})
    assert "YOUR OWN file space" in result
    assert "write_file" in tools["write"].invoke({"path": "/memories/notes.md", "content": "x"})
    assert "edit_file" in tools["edit"].invoke({"path": "/memories/notes.md", "old_string": "a", "new_string": "b"})
    assert "YOUR OWN file space" in await tools["describe_image"].ainvoke({"path": "/skills/x/shot.png"})
    # Nothing was written into the repo.
    assert not (tmp_path / "memories").exists()


async def test_a_relative_skills_dir_in_the_repo_is_still_readable(tmp_path):
    """Only ABSOLUTE own-space prefixes redirect -- a repo may have its own skills/."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "notes.md").write_text("repo skill notes", encoding="utf-8")
    tools = _tools(tmp_path)
    assert await tools["read"].ainvoke({"path": "skills/notes.md"}) == "repo skill notes"
    assert await tools["read"].ainvoke({"path": "/workspace/skills/notes.md"}) == "repo skill notes"


async def test_a_real_escape_still_gets_the_escape_error(tmp_path):
    result = await _tools(tmp_path)["read"].ainvoke({"path": "/etc/passwd"})
    assert result.startswith("ERROR:") and "RELATIVE" in result
