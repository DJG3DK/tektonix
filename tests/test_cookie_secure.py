"""The session cookie is Secure everywhere except plain http to loopback
(2026-10-04: the Linux app's WebKitGTK dropped a Secure cookie on
http://localhost, and no sign-in ever stuck)."""

import pytest
from starlette.requests import Request

from agent.routers.auth import cookie_secure


def _req(host: str, scheme: str = "http", forwarded: str | None = None) -> Request:
    headers = [(b"host", host.encode())]
    if forwarded is not None:
        headers.append((b"x-forwarded-proto", forwarded.encode()))
    return Request({"type": "http", "scheme": scheme, "headers": headers, "path": "/",
                    "server": ("127.0.0.1", 8100), "method": "POST", "query_string": b""})


@pytest.mark.parametrize("host", ["localhost:8100", "127.0.0.1:8100", "localhost", "[::1]:8100", "LOCALHOST:8100"])
def test_plain_http_to_loopback_is_not_secure(host):
    assert cookie_secure(_req(host)) is False


@pytest.mark.parametrize("req", [
    _req("agent.example.com"),                          # nginx in front: Host is the public name
    _req("localhost:8100", forwarded="https"),          # a TLS proxy that keeps Host
    _req("localhost:8100", scheme="https"),
    _req("192.168.1.20:8100"),                          # another machine on the LAN
    _req("localhost.evil.example:8100"),
])
def test_everything_else_stays_secure(req):
    assert cookie_secure(req) is True


def test_no_request_means_secure():
    assert cookie_secure(None) is True
