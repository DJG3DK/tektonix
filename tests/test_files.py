"""Unit tests for agent/tools/files.py's str_replace, in particular the
whitespace-mismatch error message: a model can guess a wrong indent width for
an edit target it only partially read, get "old_string not found", then
retry the identical string instead of re-checking. The error message detects
and names this specific failure mode -- a whitespace-insensitive match
existing -- so the next attempt doesn't need luck to recover.
"""

import pytest

from agent.tools.files import BinaryFileError, read_file, str_replace


def _write(tmp_path, rel_path: str, content: str) -> None:
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_exact_match_succeeds(tmp_path):
    _write(tmp_path, "a.txt", "hello world\n")
    str_replace(str(tmp_path), "a.txt", "hello", "goodbye")
    assert (tmp_path / "a.txt").read_text() == "goodbye world\n"


def test_indentation_mismatch_gets_the_specific_whitespace_error(tmp_path):
    """old_string uses 4-space indent, the real file uses 2-space -- must
    name whitespace as the likely cause, not just say "not found"."""
    _write(tmp_path, "a.tsx", "  useEffect(() => {\n    doThing();\n  }, [x]);\n")
    with pytest.raises(ValueError) as exc:
        str_replace(str(tmp_path), "a.tsx", "    useEffect(() => {\n      doThing();\n    }, [x]);", "new")
    assert "whitespace-insensitive match exists" in str(exc.value)
    assert "indentation" in str(exc.value)


def test_genuinely_absent_content_gets_the_generic_stale_read_error(tmp_path):
    """No match even ignoring whitespace -- a different failure mode (stale/
    incomplete read), must get the OTHER message, not the whitespace one."""
    _write(tmp_path, "a.txt", "the actual content\n")
    with pytest.raises(ValueError) as exc:
        str_replace(str(tmp_path), "a.txt", "completely different text", "new")
    assert "not even ignoring whitespace" in str(exc.value)
    assert "whitespace-insensitive match exists" not in str(exc.value)


def test_whitespace_error_never_writes_the_file(tmp_path):
    """The diagnosis upgrade must never become a fuzzy-match WRITE -- only
    the error message changes, the file must be untouched."""
    original = "  useEffect(() => {\n    doThing();\n  }, [x]);\n"
    _write(tmp_path, "a.tsx", original)
    with pytest.raises(ValueError):
        str_replace(str(tmp_path), "a.tsx", "    useEffect(() => {\n      doThing();\n    }, [x]);", "new")
    assert (tmp_path / "a.tsx").read_text() == original


def test_non_unique_match_still_raises_as_before(tmp_path):
    _write(tmp_path, "a.txt", "dup\ndup\n")
    with pytest.raises(ValueError) as exc:
        str_replace(str(tmp_path), "a.txt", "dup", "new")
    assert "not unique" in str(exc.value)


# ---------------------------------------------------------------------------
# binary-file guard: reading a binary file as text used to silently decode
# it into a same-length wall of garbage (errors="replace"), which sailed
# straight through the size caps (real image bytes routinely land under a
# threshold tuned for legitimate large source files) and then sat in
# conversation history forever, making every later call in that thread more
# expensive as it got resent.
# ---------------------------------------------------------------------------


def _write_bytes(tmp_path, rel_path: str, content: bytes):
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_read_file_refuses_binary_content(tmp_path):
    _write_bytes(tmp_path, "image.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    with pytest.raises(BinaryFileError) as exc:
        read_file(str(tmp_path), "image.png")
    assert "describe_image" in str(exc.value)


def test_read_file_allows_ordinary_text(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("just plain text\n", encoding="utf-8")
    assert read_file(str(tmp_path), "a.txt") == "just plain text\n"


def test_str_replace_refuses_binary_content(tmp_path):
    _write_bytes(tmp_path, "image.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    with pytest.raises(BinaryFileError):
        str_replace(str(tmp_path), "image.png", "x", "y")


def test_a_second_identical_noop_edit_in_a_row_names_the_already_applied_edit(tmp_path):
    """A coder re-sent the same already-applied edit four times (2026-09-25):
    the first refusal stays as it was, the second says the text is already
    there."""
    import asyncio

    from agent.tools.agent_tools import make_agent_tools
    (tmp_path / "a.py").write_text("x = 2\n")
    tools = {t.name: t for t in make_agent_tools(str(tmp_path))[0]}

    def edit(**kw):
        t = tools["edit"]
        return t.invoke(kw) if not t.coroutine else asyncio.run(t.ainvoke(kw))

    first = edit(path="a.py", old_string="x = 2", new_string="x = 2")
    assert "identical, so this edit would change nothing" in first
    second = edit(path="a.py", old_string="x = 2", new_string="x = 2")
    assert "already in the file exactly as your new_string" in second and "Read the file and move on" in second
    assert "identical, so this edit would change nothing" not in second
    # a real edit in between resets it; a different path is its own count
    assert edit(path="a.py", old_string="x = 2", new_string="x = 3") == "OK"
    assert "identical, so this edit would change nothing" in edit(path="a.py", old_string="x = 3", new_string="x = 3")
    (tmp_path / "b.py").write_text("y\n")
    assert "identical, so this edit would change nothing" in edit(path="b.py", old_string="y", new_string="y")
