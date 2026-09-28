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
