"""The local checkout is a cache of GitHub, not the source of truth.

Without a fetch, a task branches from whatever the machine last saw, does good
work against it, and finds out at merge time. The rebase path handles that when
it happens; this makes it happen less.

Real repositories and a real file:// remote. A mocked git would test the mock.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from agent.tools.git import fetch_base_from_origin


def _g(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout.strip()


def _init(path, name="t"):
    path.mkdir(parents=True, exist_ok=True)
    _g(path, "init", "-q", "-b", "main")
    _g(path, "config", "user.email", f"{name}@example.com")
    _g(path, "config", "user.name", name)
    return path


@pytest.fixture
def pair(tmp_path):
    """An `upstream` standing in for GitHub, and a `local` clone of it."""
    up = _init(tmp_path / "upstream", "up")
    (up / "app.py").write_text("v1\n")
    _g(up, "add", "-A")
    _g(up, "commit", "-qm", "first")

    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(up), str(local)], check=True)
    _g(local, "config", "user.email", "local@example.com")
    _g(local, "config", "user.name", "local")
    return up, local


def test_a_local_copy_behind_the_remote_is_brought_forward(pair):
    up, local = pair
    (up / "app.py").write_text("v2 from somebody else\n")
    _g(up, "add", "-A")
    _g(up, "commit", "-qm", "upstream moved")

    r = asyncio.run(fetch_base_from_origin(str(local)))

    assert r["ok"] and r["fetched"] and r["advanced"]
    assert _g(local, "rev-parse", "main") == _g(up, "rev-parse", "main")
    assert (local / "app.py").read_text() == "v2 from somebody else\n", "the tree followed"


def test_an_up_to_date_copy_is_left_alone(pair):
    _, local = pair
    before = _g(local, "rev-parse", "main")
    r = asyncio.run(fetch_base_from_origin(str(local)))
    assert r["ok"] and r["fetched"] and r["advanced"] is False
    assert _g(local, "rev-parse", "main") == before


def test_local_commits_are_never_rewritten(pair):
    """Somebody committed here directly. Fast-forward is impossible and this
    function has no business merging or rebasing on their behalf -- it says so
    and leaves it, and the task proceeds from the local tip."""
    up, local = pair
    (up / "app.py").write_text("theirs\n")
    _g(up, "add", "-A")
    _g(up, "commit", "-qm", "upstream")

    (local / "mine.py").write_text("local work\n")
    _g(local, "add", "-A")
    _g(local, "commit", "-qm", "committed straight to local main")
    before = _g(local, "rev-parse", "main")

    r = asyncio.run(fetch_base_from_origin(str(local)))

    assert r["ok"] and r.get("diverged") is True
    assert r["advanced"] is False
    assert _g(local, "rev-parse", "main") == before, "local history untouched"
    assert (local / "mine.py").exists()


def test_a_repo_with_no_remote_is_not_an_error(tmp_path):
    """Plenty of projects are local-only, and a task against one must run
    exactly as it did before."""
    solo = _init(tmp_path / "solo")
    (solo / "a.txt").write_text("x\n")
    _g(solo, "add", "-A")
    _g(solo, "commit", "-qm", "only commit")

    r = asyncio.run(fetch_base_from_origin(str(solo)))
    assert r["ok"] is True and r["fetched"] is False
    assert "no origin" in r["reason"]


def test_an_unreachable_remote_does_not_fail_the_task(tmp_path):
    """Offline, a VPN down, a dead host. None of these is a reason to refuse to
    work on a repository that is sitting right there."""
    solo = _init(tmp_path / "offline")
    (solo / "a.txt").write_text("x\n")
    _g(solo, "add", "-A")
    _g(solo, "commit", "-qm", "only commit")
    _g(solo, "remote", "add", "origin", str(tmp_path / "does-not-exist"))
    before = _g(solo, "rev-parse", "main")

    r = asyncio.run(fetch_base_from_origin(str(solo)))

    assert r["ok"] is True and r["fetched"] is False
    assert "fetch failed" in r["reason"]
    assert _g(solo, "rev-parse", "main") == before


def test_the_branch_moves_even_when_it_is_not_checked_out(pair):
    """`main` is checked out in the live worktree while a task runs in another
    worktree of the same repo. The ref still has to move, without touching
    anybody's working tree."""
    up, local = pair
    _g(local, "checkout", "-qb", "somewhere-else")
    (up / "app.py").write_text("moved\n")
    _g(up, "add", "-A")
    _g(up, "commit", "-qm", "upstream moved")

    r = asyncio.run(fetch_base_from_origin(str(local)))

    assert r["advanced"] is True
    assert _g(local, "rev-parse", "main") == _g(up, "rev-parse", "main")
    assert _g(local, "rev-parse", "--abbrev-ref", "HEAD") == "somewhere-else", "still where it was"
