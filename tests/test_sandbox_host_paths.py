"""Bind-mount sources are resolved by the Docker daemon, not by this process.

While the agent runs directly on the host those are the same thing. They stop
being the same thing the moment the agent runs in a container with the host's
Docker socket mounted (docs/roadmap-packaging.md, M1): the sandbox containers
are siblings created by the HOST daemon, so a `-v` naming a path that exists
only inside the agent container mounts an empty directory — and the task then
reads an empty repo with no error raised anywhere. That failure is silent,
which is why it gets tests before the bundle that needs it.

The mount TARGET is deliberately not mapped. A worktree's `.git` is a pointer
file naming an absolute path, so the container has to see the live repo at
exactly that path; only the source side changes.
"""

from __future__ import annotations

import pytest

from agent.tools import sandbox


@pytest.fixture(autouse=True)
def _no_map(monkeypatch):
    monkeypatch.delenv("AGENT_HOST_PATH_MAP", raising=False)


def test_no_mapping_is_the_identity(monkeypatch):
    """The normal single-host install must be untouched by any of this."""
    for p in ("/home/agent-workspaces/proj", "/srv/x/.git", "/tmp"):
        assert sandbox.host_path(p) == p


def test_a_windows_host_gets_a_windows_path(monkeypatch):
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", r"/projects=C:\dev")
    assert sandbox.host_path("/projects/webapp") == r"C:\dev\webapp"
    assert sandbox.host_path("/projects/webapp/.git") == r"C:\dev\webapp\.git"


def test_a_linux_host_keeps_forward_slashes(monkeypatch):
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", "/projects=/srv/repos")
    assert sandbox.host_path("/projects/a/b") == "/srv/repos/a/b"


def test_the_prefix_matches_whole_segments_only(monkeypatch):
    """A prefix of /projects must not rewrite /projects-old — that would mount
    one project's tree into another's container."""
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", "/projects=/srv/repos")
    assert sandbox.host_path("/projects-old/x") == "/projects-old/x"
    assert sandbox.host_path("/projectsfoo") == "/projectsfoo"


def test_the_mapped_root_itself_maps(monkeypatch):
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", "/projects=/srv/repos")
    assert sandbox.host_path("/projects") == "/srv/repos"


def test_a_nested_mapping_wins_over_its_parent(monkeypatch):
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", "/projects=/srv/a,/projects/big=/mnt/fast")
    assert sandbox.host_path("/projects/big/repo") == "/mnt/fast/repo"
    assert sandbox.host_path("/projects/small/repo") == "/srv/a/small/repo"


def test_junk_entries_are_ignored_rather_than_fatal(monkeypatch):
    """A malformed variable must degrade to the identity, not stop the agent
    from running a single command."""
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", "not-a-pair,,=,/projects=/srv/repos,=/x,/y=")
    assert sandbox.host_path("/projects/a") == "/srv/repos/a"
    assert sandbox.host_path("/elsewhere") == "/elsewhere"


def test_an_empty_path_is_returned_unchanged(monkeypatch):
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", "/projects=/srv/repos")
    assert sandbox.host_path("") == ""


# ---------------------------------------------------------------------------
# how it reaches the actual docker argv
# ---------------------------------------------------------------------------

def test_every_bind_mount_source_goes_through_the_map():
    """Five `-v` sources: the workspace for a command and again for a preview,
    the workspace at a project's extra mount points (sandbox_mounts: /testbed
    for SWE-bench), the live .git of a worktree, and the borrowed node_modules. A new one
    added without the map is the silent bug this whole module exists to
    prevent -- the host daemon resolves the source, so a container-only path
    mounts an empty directory and says nothing.

    The count is here so that adding a mount is a decision rather than an
    accident; the assertion that matters is the one below it."""
    import inspect
    src = inspect.getsource(sandbox)
    mounts = [ln for ln in src.split("\n") if '"-v"' in ln]
    assert len(mounts) == 5, f"a bind mount was added or removed: {mounts}"
    for ln in mounts:
        assert "host_path(" in ln, f"bind mount source not mapped: {ln.strip()}"


def test_the_container_side_is_not_mapped():
    """The target must stay the path the .git pointer names, or git inside the
    container cannot resolve the worktree at all."""
    import inspect
    src = inspect.getsource(sandbox)
    assert '["-v", f"{host_path(main_git)}:{main_git}:ro"]' in src
    assert '"-v", f"{host_path(cwd)}:/workspace"' in src
