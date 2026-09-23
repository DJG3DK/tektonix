"""A 304 for a dist-root file carries no body and no Content-Length.

The revalidation answer used to copy every header of the full FileResponse,
content-length included, and uvicorn raised "Response content shorter than
Content-Length" on each one -- hundreds a day from browsers re-checking the
app icon.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.server as srv

ICON = srv.FRONTEND_DIST / "icon-192.png"

pytestmark = pytest.mark.skipif(not ICON.is_file(), reason="frontend not built")


@pytest.fixture(scope="module")
def client():
    # raise_server_exceptions surfaces the ASGI error that production only logs.
    return TestClient(srv.app, raise_server_exceptions=True)


def test_first_fetch_is_the_file(client):
    r = client.get("/icon-192.png")
    assert r.status_code == 200
    assert r.content == ICON.read_bytes()
    assert r.headers["etag"]


@pytest.mark.parametrize("validator", ["etag", "last-modified"])
def test_revalidation_is_a_bodyless_304(client, validator):
    first = client.get("/icon-192.png")
    header = {"etag": "if-none-match", "last-modified": "if-modified-since"}[validator]
    r = client.get("/icon-192.png", headers={header: first.headers[validator]})
    assert r.status_code == 304
    assert r.content == b""
    assert "content-length" not in r.headers
    # Still enough for the browser to keep using its copy.
    assert r.headers["etag"] == first.headers["etag"]
    assert r.headers["cache-control"] == "public, no-cache"
