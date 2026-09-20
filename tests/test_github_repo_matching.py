"""A repository that moved must still be recognised as the project it already is.

Transferring a repository to an organisation changes its `owner/repo` path.
Every checkout made before the move keeps the old one, and GitHub goes on
serving it by redirect -- so the remote works, `git push` works, and the only
thing that breaks is matching: the repository list asked GitHub what the token
reaches, got the NEW path back, found no project with that remote, and offered
to add a project that was already there. Clicking that cloned a second copy
beside the live checkout, and two projects then pointed at one repository.

These cover both halves of the fix: resolving the old path forward, and
refusing the duplicate at the point the clone would happen -- because the list
is built when somebody opens it and can be stale by the time they click.
"""

import subprocess

import httpx
import pytest

from agent import github_repos


def _fake_github(monkeypatch, by_path):
    """`by_path` maps an API path to (status, body). Requests to anything
    else 404, which is what an unreachable repository looks like."""
    seen = []

    class _Resp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *a, **k):
            self.follow = k.get("follow_redirects", False)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            path = url.split("api.github.com", 1)[-1]
            seen.append(path)
            status, body = by_path.get(path, (404, {"message": "Not Found"}))
            return _Resp(status, body)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


async def test_resolve_slug_follows_a_transfer(monkeypatch):
    # httpx is asked to follow the redirect, so what comes back is already the
    # repository at its new home.
    _fake_github(monkeypatch, {
        "/repos/olduser/thing": (200, {"full_name": "TheOrg/thing"}),
    })
    assert await github_repos.resolve_slug("tok", "olduser/thing") == "TheOrg/thing"


async def test_resolve_slug_is_none_when_the_token_cannot_see_it(monkeypatch):
    _fake_github(monkeypatch, {})
    assert await github_repos.resolve_slug("tok", "someone/private") is None


async def test_resolve_slug_survives_a_network_failure(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    # A repository list that cannot reach GitHub is a worse answer than one
    # that reports the slug unmatched, so this never raises.
    assert await github_repos.resolve_slug("tok", "a/b") is None


def _repo_with_origin(tmp_path, name, origin):
    d = tmp_path / name
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=d, check=True)
    return str(d)


@pytest.fixture
def two_projects(monkeypatch, tmp_path):
    """One project whose checkout predates a transfer, one that never moved."""
    from agent import server

    moved = _repo_with_origin(tmp_path, "moved", "git@github.com:olduser/Thing.git")
    stayed = _repo_with_origin(tmp_path, "stayed", "https://github.com/TheOrg/Other.git")
    monkeypatch.setattr(server, "PROJECTS", {
        "thing": {"live": moved},
        "other": {"live": stayed},
        "no-checkout": {},
    }, raising=False)
    return server


def test_remote_slugs_read_the_checkouts_not_the_config(two_projects):
    slugs = two_projects._project_remote_slugs()
    assert slugs == {"olduser/thing": "thing", "theorg/other": "other"}


async def test_onboarded_as_matches_without_asking_github(monkeypatch, two_projects):
    seen = _fake_github(monkeypatch, {})
    assert await two_projects._onboarded_as("TheOrg/Other", "tok") == "other"
    assert seen == [], "an exact match must not cost an API call"


async def test_onboarded_as_finds_a_project_whose_repo_was_transferred(monkeypatch, two_projects):
    _fake_github(monkeypatch, {
        "/repos/olduser/thing": (200, {"full_name": "TheOrg/Thing"}),
    })
    # This is the click that made a second copy: the list offered TheOrg/Thing
    # because no project's remote said that, but `thing` IS that repository.
    assert await two_projects._onboarded_as("TheOrg/Thing", "tok") == "thing"


async def test_onboarded_as_says_no_for_a_genuinely_new_repository(monkeypatch, two_projects):
    _fake_github(monkeypatch, {
        "/repos/olduser/thing": (200, {"full_name": "TheOrg/Thing"}),
    })
    assert await two_projects._onboarded_as("TheOrg/Brand-New", "tok") is None


async def test_onboarded_as_without_a_token_still_matches_outright(two_projects):
    # No token means no resolving, but an exact match needs none.
    assert await two_projects._onboarded_as("olduser/Thing", None) == "thing"
    assert await two_projects._onboarded_as("TheOrg/Thing", None) is None
