"""Onboarding a project that exists nowhere but GitHub.

The clone lands where a new project would, under the same containment rule,
and the caller then runs the ordinary detection flow against the path. Nothing
downstream knows how the directory got there, which is the point.
"""
from __future__ import annotations

import subprocess

import pytest

from agent import provisioning
from agent.provisioning import ProvisioningError, clone_repository, parse_github_source


@pytest.mark.parametrize("text,expected", [
    ("https://github.com/owner/repo", ("owner", "repo")),
    ("https://github.com/owner/repo.git", ("owner", "repo")),
    ("https://github.com/owner/repo/", ("owner", "repo")),
    ("https://www.github.com/owner/repo", ("owner", "repo")),
    ("git@github.com:owner/repo.git", ("owner", "repo")),
    ("ssh://git@github.com/owner/repo", ("owner", "repo")),
    ("owner/repo", ("owner", "repo")),
    ("owner/repo.git", ("owner", "repo")),
])
def test_the_shapes_a_person_actually_pastes(text, expected):
    assert parse_github_source(text) == expected


@pytest.mark.parametrize("text", [
    "/home/me/code/thing",      # the wizard's existing input, and the reason
    "./relative",               # None means "treat it as a path", so guessing
    "~/code/thing",             # wrong here is how you clone a directory name
    "",
    "https://gitlab.com/owner/repo",
    "https://example.com/owner/repo",
    "../escape",
])
def test_anything_that_is_not_a_github_repo_is_left_as_a_path(text):
    assert parse_github_source(text) is None


@pytest.fixture
def upstream(tmp_path):
    """A bare repo standing in for GitHub, reachable over file://."""
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.email", "u@example.com"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.name", "u"], cwd=src, check=True)
    (src / "package.json").write_text('{"name":"thing","scripts":{"test":"echo ok"}}')
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=src, check=True)
    return src


def test_a_clone_lands_in_the_allowed_root_and_is_a_real_repo(tmp_path, upstream, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(provisioning, "allowed_roots", lambda: [str(root)])
    # The URL parsing is covered above; here the network is the part that
    # cannot be real, so the clone runs against a local path instead.
    monkeypatch.setattr(provisioning, "_run_git", _git_with_local_source(str(upstream)))

    path = clone_repository("someone/thing", str(root))

    assert path == str((root / "thing").resolve())
    assert (root / "thing" / ".git").is_dir()
    assert (root / "thing" / "package.json").is_file(), "the content came with it"


def _git_with_local_source(local: str, seen: list | None = None):
    """Point the github.com URL at a local path on the CLONE only, with a
    one-command insteadOf so the remote git stores is still the URL it was
    given -- which is what the token test is about. The clone is then real
    (objects, refs, a working tree) without a network."""
    real = provisioning._run_git

    def fake(args, cwd=None, **kw):
        if seen is not None:
            seen.append((list(args), kw.get("extra_env")))
        if "clone" in args:
            url = next(a for a in args if isinstance(a, str) and "github.com" in a)
            args = ["-c", f"url.{local}.insteadOf={url}", *args]
        return real(args, cwd=cwd, **kw)

    return fake


def test_it_refuses_to_clone_over_something_that_is_already_there(tmp_path, upstream, monkeypatch):
    root = tmp_path / "projects"
    (root / "thing").mkdir(parents=True)
    monkeypatch.setattr(provisioning, "allowed_roots", lambda: [str(root)])
    with pytest.raises(ProvisioningError) as e:
        clone_repository("someone/thing", str(root))
    assert "already exists" in str(e.value)


def test_a_path_is_refused_rather_than_cloned(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioning, "allowed_roots", lambda: [str(tmp_path)])
    with pytest.raises(ProvisioningError) as e:
        clone_repository("/home/me/code/thing", str(tmp_path))
    assert "not a GitHub URL" in str(e.value)


def test_a_failed_clone_leaves_nothing_behind(tmp_path, upstream, monkeypatch):
    """A half-made directory is the state the wizard cannot recover from: the
    next attempt refuses because the path exists."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(provisioning, "allowed_roots", lambda: [str(root)])
    monkeypatch.setattr(provisioning, "_run_git",
                        _git_with_local_source(str(tmp_path / "no-such-repo")))

    with pytest.raises(ProvisioningError):
        clone_repository("someone/thing", str(root))
    assert not (root / "thing").exists(), "cleaned up after itself"


def test_a_token_never_reaches_the_stored_remote(tmp_path, upstream, monkeypatch):
    """.git/config is readable by anyone who can read the checkout, and a
    clone URL with a token in it stays there forever."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(provisioning, "allowed_roots", lambda: [str(root)])
    monkeypatch.setattr(provisioning, "_run_git", _git_with_local_source(str(upstream)))

    path = clone_repository("someone/thing", str(root), token="ghp_secret_value")

    config = (tmp_path / "projects" / "thing" / ".git" / "config").read_text()
    assert "ghp_secret_value" not in config
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=path,
                            capture_output=True, text=True).stdout.strip()
    assert remote == "https://github.com/someone/thing.git"


def test_a_token_is_never_on_the_clone_command_line(tmp_path, upstream, monkeypatch):
    """Every local user can read a process's argv for as long as it runs."""
    from agent.tools.git import GIT_TOKEN_ENV
    root = tmp_path / "projects"
    root.mkdir()
    seen: list = []
    monkeypatch.setattr(provisioning, "allowed_roots", lambda: [str(root)])
    monkeypatch.setattr(provisioning, "_run_git", _git_with_local_source(str(upstream), seen))

    clone_repository("someone/thing", str(root), token="ghp_secret_value")

    (args, env), = [(a, e) for a, e in seen if "clone" in a]
    assert not any("ghp_secret_value" in a for a in args)
    assert env[GIT_TOKEN_ENV] == "ghp_secret_value"


def test_a_clone_that_times_out_does_not_report_its_argv(tmp_path, monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["git", "clone", "https://x:ghp_secret_value@h"], timeout=1)

    monkeypatch.setattr(subprocess, "run", hang)
    ok, out = provisioning._run_git(["clone", "x"], cwd=str(tmp_path), timeout=1)
    assert ok is False and "timed out" in out
    assert "ghp_secret_value" not in out


@pytest.mark.parametrize("text", ["relative/path", "src/components", "owner/repo"])
def test_a_bare_slug_is_ambiguous_and_the_two_callers_answer_it_differently(text):
    """`owner/repo` and `relative/path` are the same string. Nothing can tell
    them apart, so Clone accepts the bare form because the operator chose that
    action against that input, and Detect does not -- a relative path there
    still gets "must be absolute" rather than an offer to clone something that
    does not exist.

    Found by a test: adding the slug form made Detect call `relative/path` a
    GitHub repository.
    """
    assert parse_github_source(text) is not None, "Clone accepts it"
    assert parse_github_source(text, allow_slug=False) is None, "Detect does not"


@pytest.mark.parametrize("text", [
    "https://github.com/owner/repo",
    "git@github.com:owner/repo.git",
])
def test_an_unmistakable_url_is_recognised_by_both(text):
    assert parse_github_source(text) is not None
    assert parse_github_source(text, allow_slug=False) is not None


# --- how a cloned project ships ---------------------------------------------

def test_a_cloned_project_opens_pull_requests_by_default():
    """The question this answers: if the agent edits a repository it cloned,
    does it push to main or open a PR?

    It opens a PR. A path the operator already had checked out is one they own
    and deploy, and merging into its base branch is the point. A repository
    cloned because somebody pasted a URL is not that: nobody asked the agent to
    own it, and its base branch may carry other people's work. Pushing straight
    to it would be the surprising answer.
    """
    entry = provisioning.config_from_choices(
        "/srv/live/widget", "/srv/ws/widget",
        {"cloned_from_github": True},
    )
    assert entry["ship"] == "pr"


def test_a_project_onboarded_from_a_local_path_still_pushes():
    """Every existing project keeps doing exactly what it did."""
    entry = provisioning.config_from_choices(
        "/srv/live/widget", "/srv/ws/widget", {},
    )
    assert "ship" not in entry, "absent means push, which is the documented default"


def test_the_operator_can_override_either_way():
    """A default, not a rule."""
    cloned_but_push = provisioning.config_from_choices(
        "/l", "/s", {"cloned_from_github": True, "ship": "push"})
    assert cloned_but_push["ship"] == "push"

    local_but_pr = provisioning.config_from_choices("/l", "/s", {"ship": "pr"})
    assert local_but_pr["ship"] == "pr"


def test_an_unrecognised_ship_value_is_dropped_rather_than_stored():
    """projects.json is read by three processes. A value none of them
    understands is worse than the default."""
    class _R:
        live = "/srv/live/w"
        checks: list = []
        risky_scripts: list = []
        build_steps: list = []
        secret_files: list = []
        read_only_mounts: list = []
        pm2_apps: list = []
        node_modules_dirs: list = []
        dependency_dirs: list = []
        restart_commands: list = []

    clean = provisioning.validate_choices(_R(), {"ship": "yolo"})
    assert "ship" not in clean
