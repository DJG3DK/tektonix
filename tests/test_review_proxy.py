"""The console reaching the review services without nginx.

They hold the only write path into a live repo, so they bind loopback and
publish nothing. On a host install nginx bridges /_review/ and injects the
shared secret the browser must never hold. The container bundle has no nginx,
so the gate ran and the agent drove it while a person could not.

These check the two things a proxy must not get wrong: who is allowed through,
and what it does with a header the caller supplied.
"""
from __future__ import annotations

import os

import httpx
import pytest

from agent import server as srv


class _Recorder:
    """Stands in for the review service, and remembers what arrived."""

    def __init__(self, status=200, body=b'{"ok":true}', content_type="application/json"):
        self.status, self.body, self.content_type = status, body, content_type
        self.seen: dict = {}

    async def __call__(self, method, url, params=None, content=None, headers=None):
        self.seen = {
            "method": method, "url": str(url), "params": dict(params or {}),
            "content": content, "headers": {k.lower(): v for k, v in (headers or {}).items()},
        }
        return httpx.Response(
            self.status, content=self.body,
            headers={"content-type": self.content_type},
        )


@pytest.fixture
def upstream(monkeypatch):
    rec = _Recorder()

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kw): return await rec(method, url, **kw)

    monkeypatch.setattr(srv.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("REVIEW_CONTROL_SECRET", "the-real-secret")
    return rec


async def _call(path="api/projects", method="GET", headers=None, body=None, query=""):
    """Drive the endpoint directly: the auth dependency is what the other tests
    cover, and standing up the whole app here would test FastAPI."""
    scope_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": body or b"", "more_body": False}

    req = Request({
        "type": "http", "method": method, "path": f"/_review/{path}",
        "query_string": query.encode(), "headers": scope_headers,
    }, receive)
    return await srv.review_proxy(path=path, request=req, user=_ADMIN)


class _User:
    id = 1
    email = "admin@example.com"
    role = "admin"
    is_admin = True


_ADMIN = _User()


@pytest.mark.asyncio
async def test_the_secret_is_injected_not_forwarded(upstream, monkeypatch):
    """The browser never holds this value, and a caller who sends one must not
    be able to choose what the review service sees. Authority here comes from
    the session, not from a header somebody typed."""
    monkeypatch.setattr(srv.auth, "require_admin", lambda u: None)
    await _call(headers={"X-Review-Secret": "attacker-supplied", "Accept": "application/json"})

    sent = upstream.seen["headers"]
    assert sent["x-review-secret"] == "the-real-secret"
    assert "attacker-supplied" not in sent.values()


@pytest.mark.asyncio
async def test_the_path_query_and_body_all_reach_the_service(upstream, monkeypatch):
    monkeypatch.setattr(srv.auth, "require_admin", lambda u: None)
    await _call(path="api/projects/demo/merge", method="POST", body=b'{"force":true}', query="dry=1")

    assert upstream.seen["url"].endswith("/api/projects/demo/merge")
    assert upstream.seen["params"] == {"dry": "1"}
    assert upstream.seen["content"] == b'{"force":true}'
    assert upstream.seen["method"] == "POST"


@pytest.mark.asyncio
async def test_hop_by_hop_headers_are_not_relayed(upstream, monkeypatch):
    """Forwarding Host or a transfer encoding onward is how a proxy produces a
    response nobody can parse."""
    monkeypatch.setattr(srv.auth, "require_admin", lambda u: None)
    await _call(headers={"Host": "console.example.com", "Connection": "keep-alive"})

    sent = upstream.seen["headers"]
    assert "host" not in sent and "connection" not in sent


@pytest.mark.asyncio
async def test_a_non_admin_is_refused_before_anything_is_forwarded(upstream, monkeypatch):
    """The review services hold the only write path into a live repo. This
    endpoint must be exactly as hard to reach as the console's own admin
    routes, not one notch easier."""
    def deny(_user):
        raise srv.HTTPException(403, "admin only")

    monkeypatch.setattr(srv.auth, "require_admin", deny)
    with pytest.raises(srv.HTTPException) as e:
        await _call()
    assert e.value.status_code == 403
    assert upstream.seen == {}, "nothing was forwarded"


@pytest.mark.asyncio
async def test_a_service_that_is_down_says_which_service(upstream, monkeypatch):
    """A bare 502 from the console reads as the console being broken."""
    monkeypatch.setattr(srv.auth, "require_admin", lambda u: None)

    class Dead:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, *a, **k): raise httpx.ConnectError("refused")

    monkeypatch.setattr(srv.httpx, "AsyncClient", Dead)
    r = await _call()
    assert r.status_code == 502
    assert b"review service is not reachable" in r.body


@pytest.mark.asyncio
async def test_the_upstream_status_and_body_come_back_unchanged(monkeypatch):
    """A 409 from the merge endpoint carries the reason the console shows. If
    this flattened everything to 200 or 500, the reason would be lost."""
    rec = _Recorder(status=409, body=b'{"ok":false,"reason":"diverged"}')

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kw): return await rec(method, url, **kw)

    monkeypatch.setattr(srv.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(srv.auth, "require_admin", lambda u: None)
    monkeypatch.setenv("REVIEW_CONTROL_SECRET", "s")

    r = await _call(path="api/projects/demo/merge", method="POST")
    assert r.status_code == 409
    assert b"diverged" in r.body


@pytest.mark.asyncio
async def test_no_secret_configured_forwards_without_one(upstream, monkeypatch):
    """Fail at the review service's own gate, which answers 401 with a reason,
    rather than sending the literal string None."""
    monkeypatch.setattr(srv.auth, "require_admin", lambda u: None)
    monkeypatch.delenv("REVIEW_CONTROL_SECRET", raising=False)
    await _call()
    assert "x-review-secret" not in upstream.seen["headers"]
