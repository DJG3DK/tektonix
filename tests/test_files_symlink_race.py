"""read/write/edit cannot be redirected out of the repo by a symlink swapped
in after the containment check.

_resolve checks, the caller opens: between the two, the agent's sandboxed
shell -- which writes the same workspace concurrently -- could replace a
directory with a symlink to anywhere the agent server can read. Measured
before the fix: roughly one read in four returned a file outside the repo
under a tight swap loop, and a write landed outside it.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from agent.tools import files


@pytest.fixture
def layout(tmp_path):
    ws = tmp_path / "ws"
    (ws / "d").mkdir(parents=True)
    (ws / "d" / "f.txt").write_text("inside")
    out = tmp_path / "outside"
    out.mkdir()
    (out / "f.txt").write_text("HOST SECRET")
    return ws, out


def _swap_after_resolve(monkeypatch, ws, out):
    """Make _resolve return its (valid) answer, then swap `d` for a link --
    the exact window the race used, made deterministic."""
    real = files._resolve

    def resolve_then_swap(root, rel):
        target = real(root, rel)
        os.rename(ws / "d", ws / "d_real")
        os.symlink(out, ws / "d")
        return target

    monkeypatch.setattr(files, "_resolve", resolve_then_swap)


def test_a_directory_swapped_for_a_link_after_the_check_is_refused_on_read(layout, monkeypatch):
    ws, out = layout
    _swap_after_resolve(monkeypatch, ws, out)
    with pytest.raises(files.PathEscapeError):
        files.read_file(str(ws), "d/f.txt")


def test_a_directory_swapped_for_a_link_after_the_check_is_refused_on_write(layout, monkeypatch):
    ws, out = layout
    _swap_after_resolve(monkeypatch, ws, out)
    with pytest.raises(files.PathEscapeError):
        files.write_file(str(ws), "d/f.txt", "PWNED")
    assert (out / "f.txt").read_text() == "HOST SECRET"


def test_a_file_swapped_for_a_link_after_the_check_is_refused(layout, monkeypatch):
    ws, out = layout
    real = files._resolve

    def resolve_then_swap(root, rel):
        target = real(root, rel)
        os.unlink(ws / "d" / "f.txt")
        os.symlink(out / "f.txt", ws / "d" / "f.txt")
        return target

    monkeypatch.setattr(files, "_resolve", resolve_then_swap)
    with pytest.raises(files.PathEscapeError):
        files.str_replace(str(ws), "d/f.txt", "SECRET", "x")
    assert (out / "f.txt").read_text() == "HOST SECRET"


def test_under_a_real_swap_loop_nothing_outside_is_read_or_written(layout):
    ws, out = layout
    stop = threading.Event()

    def swapper():
        while not stop.is_set():
            try:
                os.rename(ws / "d", ws / "d_real")
                os.symlink(out, ws / "d")
                os.unlink(ws / "d")
                os.rename(ws / "d_real", ws / "d")
            except OSError:
                pass

    t = threading.Thread(target=swapper, daemon=True)
    t.start()
    leaked = reads = 0
    deadline = time.monotonic() + 1.5
    try:
        while time.monotonic() < deadline:
            try:
                if "HOST SECRET" in files.read_file(str(ws), "d/f.txt"):
                    leaked += 1
                reads += 1
            except (files.PathEscapeError, OSError):
                pass
            try:
                files.write_file(str(ws), "d/f.txt", "PWNED")
            except (files.PathEscapeError, OSError):
                pass
    finally:
        stop.set()
        t.join()
    assert leaked == 0, f"{leaked} of {reads} reads returned a file outside the repo"
    assert (out / "f.txt").read_text() == "HOST SECRET", "a write landed outside the repo"


def test_ordinary_use_is_unchanged(layout):
    ws, _out = layout
    files.write_file(str(ws), "new/deep/dir/a.txt", "hello")
    assert files.read_file(str(ws), "new/deep/dir/a.txt") == "hello"
    files.str_replace(str(ws), "new/deep/dir/a.txt", "hello", "bye")
    assert (ws / "new/deep/dir/a.txt").read_text() == "bye"
    # A link that stays inside the repo still works: _resolve follows it.
    os.symlink(ws / "d", ws / "alias")
    assert files.read_file(str(ws), "alias/f.txt") == "inside"
    with pytest.raises(IsADirectoryError):
        files.read_file(str(ws), "d")
    with pytest.raises(FileNotFoundError):
        files.read_file(str(ws), "missing.txt")


def test_a_fifo_cannot_hang_a_read(layout):
    ws, _out = layout
    os.mkfifo(ws / "pipe")
    with pytest.raises(IsADirectoryError):
        files.read_file(str(ws), "pipe")
