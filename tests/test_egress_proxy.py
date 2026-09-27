"""The browser's egress proxy: the address checked is the address connected to.

url_guard resolved a host, approved it, and let Chromium resolve it again to
connect. A name answering public-then-127.0.0.1 (DNS rebinding) passed the
check and reached loopback. These drive the proxy over real sockets, and the
last ones drive a real headless Chromium through it when one is installed.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from agent.tools import egress_proxy
from agent.tools.egress_proxy import REFUSED_HEADER, EgressProxy

PUBLIC = "93.184.216.34"


@contextlib.asynccontextmanager
async def _local_http(body: bytes = b"SECRET-ON-LOOPBACK"):
    """A loopback HTTP server that records every request head it receives."""
    seen: list[bytes] = []

    async def handle(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], seen
    finally:
        server.close()
        await server.wait_closed()


def _answers(monkeypatch, *answers):
    """getaddrinfo that returns each answer in turn, then the last forever --
    a rebinding name's view of the world."""
    calls = []

    def fake(host, port, *a, **k):
        calls.append(host)
        ips = answers[min(len(calls), len(answers)) - 1]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(egress_proxy.socket, "getaddrinfo", fake)
    return calls


async def _raw(proxy: EgressProxy, request: bytes) -> bytes:
    host, port = proxy.url.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return data


async def test_a_name_that_resolves_to_loopback_is_refused(monkeypatch):
    _answers(monkeypatch, ["127.0.0.1"])
    async with _local_http() as (port, seen), EgressProxy() as proxy:
        out = await _raw(proxy, f"GET http://rebind.test:{port}/ HTTP/1.1\r\nHost: rebind.test\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.1 403")
    assert REFUSED_HEADER.encode() in out
    assert b"SECRET" not in out and seen == []
    assert "127.0.0.1" in proxy.blocked[0]


async def test_one_private_record_among_public_ones_is_refused(monkeypatch):
    _answers(monkeypatch, [PUBLIC, "10.0.0.5"])
    async with EgressProxy() as proxy:
        out = await _raw(proxy, b"CONNECT mixed.test:443 HTTP/1.1\r\nHost: mixed.test:443\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 403")


async def test_metadata_and_loopback_literals_are_refused_on_both_paths():
    async with EgressProxy() as proxy:
        for req in (b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: x\r\n\r\n",
                    b"CONNECT 127.0.0.1:4101 HTTP/1.1\r\nHost: x\r\n\r\n",
                    b"CONNECT [::1]:22 HTTP/1.1\r\nHost: x\r\n\r\n"):
            assert (await _raw(proxy, req)).startswith(b"HTTP/1.1 403"), req


async def test_it_connects_to_the_address_it_checked_and_does_not_look_again(monkeypatch):
    """The whole fix: one lookup, and the socket goes to its answer. A second
    lookup here would be the rebinding window all over again."""
    calls = _answers(monkeypatch, [PUBLIC], ["127.0.0.1"])
    connected = []

    async with _local_http(b"hello") as (port, seen):
        class Pinned(EgressProxy):
            async def connect(self, address, p):
                connected.append((address, p))
                return await asyncio.open_connection("127.0.0.1", port)

        async with Pinned() as proxy:
            out = await _raw(proxy, b"GET http://rebind.test/path?q=1 HTTP/1.1\r\nHost: rebind.test\r\n"
                                    b"Proxy-Connection: keep-alive\r\nProxy-Authorization: x\r\n\r\n")
    assert calls == ["rebind.test"], "resolved exactly once"
    assert connected == [(PUBLIC, 80)]
    assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"hello")
    head = seen[0].decode()
    assert head.startswith("GET /path?q=1 HTTP/1.1\r\n"), "origin-form upstream"
    assert "Proxy-" not in head and "Connection: close" in head


async def test_the_preview_origin_is_the_one_loopback_exception(monkeypatch):
    async with _local_http(b"preview") as (port, _):
        async with EgressProxy(allow_origin=f"http://127.0.0.1:{port}") as proxy:
            ok = await _raw(proxy, f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            other = await _raw(proxy, f"GET http://127.0.0.1:{port + 1}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert ok.endswith(b"preview")
    assert other.startswith(b"HTTP/1.1 403"), "same host, another port, is not the origin"


async def test_a_tunnel_relays_bytes_both_ways(monkeypatch):
    async with _local_http(b"through-the-tunnel") as (port, _):
        async with EgressProxy(allow_origin=f"http://127.0.0.1:{port}") as proxy:
            out = await _raw(proxy, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: x\r\n\r\n"
                                    f"GET / HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.1 200 Connection Established")
    assert out.endswith(b"through-the-tunnel")


async def test_garbage_is_refused_not_crashed_on():
    async with EgressProxy() as proxy:
        assert (await _raw(proxy, b"NONSENSE\r\n\r\n")).startswith(b"HTTP/1.1 400")
        assert (await _raw(proxy, b"GET /relative HTTP/1.1\r\nHost: x\r\n\r\n")).startswith(b"HTTP/1.1 400")


# ---------------------------------------------------------------------------
# a real browser
# ---------------------------------------------------------------------------

def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return bool(p.chromium.executable_path) and __import__("os").path.exists(p.chromium.executable_path)
    except Exception:  # noqa: BLE001
        return False


needs_chromium = pytest.mark.skipif(not _chromium_available(), reason="no Playwright Chromium installed")


@needs_chromium
async def test_a_rebinding_name_does_not_reach_loopback_through_the_browser(monkeypatch):
    """The attack end to end. The guard's lookups see a public address; the
    lookup behind the connection sees 127.0.0.1 -- Chromium's own resolver is
    mapped there, as the attacker's DNS would on its second answer. Without
    the proxy the page text was the loopback service's."""
    from agent.tools import planning_tools, url_guard

    guard_calls = []

    def guard_view(host, port, *a, **k):
        guard_calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", guard_view)
    monkeypatch.setattr(egress_proxy, "socket", _SecondAnswer("127.0.0.1"))
    monkeypatch.setattr(planning_tools, "_LAUNCH_ARGS",
                        [*planning_tools._LAUNCH_ARGS, "--host-resolver-rules=MAP rebind.test 127.0.0.1"])

    async with _local_http() as (port, seen):
        out = await planning_tools._run_browse_page(f"http://rebind.test:{port}/", False, "")
    assert "SECRET-ON-LOOPBACK" not in out, out
    assert seen == [], "nothing reached the loopback service"
    assert out.startswith("ERROR: blocked"), out
    assert guard_calls, "the entry check still ran"


@needs_chromium
async def test_a_preview_still_renders_through_the_proxy(monkeypatch):
    from agent.tools import planning_tools

    async def no_vision(*a, **k):
        return "(described)"

    monkeypatch.setattr(planning_tools, "describe_image_bytes", no_vision)
    async with _local_http(b"<html><body><h1>PREVIEW-OK</h1></body></html>") as (port, seen):
        origin = f"http://127.0.0.1:{port}"
        out = await planning_tools.run_browse_page_on_origin(origin + "/", origin)
    assert "PREVIEW-OK" in out, out
    assert seen, "the page came from the preview server"


class _SecondAnswer:
    """Stands in for the socket module inside egress_proxy: every lookup gets
    the rebinding name's later answer."""

    def __init__(self, ip):
        self._ip = ip
        self.gaierror = socket.gaierror
        self.SOCK_STREAM = socket.SOCK_STREAM

    def getaddrinfo(self, host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self._ip, port))]
