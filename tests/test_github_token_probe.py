"""The token probe says WHY a configured project is out of reach. It used to
swallow the lookup error, so a token that reached nothing read as "check
the token's repository access" with nothing to check (2026-09-28, the first
Windows install: an organisation's repository and a personal token)."""
import asyncio

from agent import github_inbox


class _Client:
    def __init__(self, token):
        pass

    async def get(self, path, params=None):
        if path == "/user":
            return {"login": "danny"}
        if path == "/user/repos":
            return [{"full_name": "danny/notes", "permissions": {"push": True, "pull": True}}]
        raise LookupError(f"GitHub 404 for {path} (no access, or it does not exist)")

    async def repo(self, slug):
        raise LookupError(f"GitHub 404 for /repos/{slug} (no access, or it does not exist)")


def test_an_unreachable_organisation_repository_is_named_with_the_reason(monkeypatch):
    monkeypatch.setattr(github_inbox, "GitHubClient", _Client)
    monkeypatch.setattr(github_inbox, "resolve_slug", lambda name: {"bot": "Some-Org/bot", "local": None}[name])
    out = asyncio.run(github_inbox.probe_token("tok", {"bot": {}, "local": {}}))
    assert out["ok"] and out["login"] == "danny" and out["matched"] == []
    by = {u["project"]: u for u in out["unreached"]}
    assert "404" in by["bot"]["error"] and "belongs to Some-Org, not to danny" in by["bot"]["error"]
    assert "resource owner" in by["bot"]["error"]
    assert by["local"]["slug"] is None and "no GitHub origin" in by["local"]["error"]


def test_two_projects_on_one_repository_are_both_named(monkeypatch):
    """The slug map was keyed slug -> project, so of two checkouts of one
    repository only the later one was reported reached, and the earlier
    looked unmatched (2026-09-29)."""
    class _Listed(_Client):
        async def get(self, path, params=None):
            if path == "/user/repos":
                return [{"full_name": "danny/notes", "permissions": {"push": True, "pull": True}}]
            return await super().get(path, params)

    monkeypatch.setattr(github_inbox, "GitHubClient", _Listed)
    monkeypatch.setattr(github_inbox, "resolve_slug", lambda name: "danny/notes")
    out = asyncio.run(github_inbox.probe_token("tok", {"notes": {}, "notes-staging": {}}))
    assert out["unreached"] == []
    assert len(out["matched"]) == 1
    assert out["matched"][0]["projects"] == ["notes", "notes-staging"]
    assert out["matched"][0]["project"] == "notes, notes-staging"


def test_a_project_without_an_origin_is_asked_again_next_time(tmp_path, monkeypatch):
    """A miss was cached, so an origin added after the first lookup read as
    "no GitHub origin" until the process restarted."""
    import subprocess

    from agent.tools import github_tools as gh

    repo = tmp_path / "later"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(gh, "PROJECTS", {"later": {"live": str(repo), "sandbox": str(repo)}})
    monkeypatch.setattr(gh, "_slug_cache", {})
    assert gh.resolve_slug("later") is None
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:danny/later.git"], check=True)
    assert gh.resolve_slug("later") == "danny/later", "the miss was cached"
    assert gh._slug_cache == {"later": "danny/later"}
