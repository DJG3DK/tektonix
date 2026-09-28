"""SSRF guard for LLM-chosen URLs (agent/tools/url_guard.py).

browse_page drives a real headless browser on the host, on a URL the model
picks. Before this guard existed there was no check at all, and both of
these were verified working against the live tool on 2026-08-24:

    file:///etc/hostname     -> returned the host's hostname
    http://127.0.0.1:4101/   -> reached the review service's control port

That port is unauthenticated because "localhost only" was treated as the
boundary, and file:// reads anything the process can read, this agent's own
.env included.
"""

import ipaddress
from unittest.mock import patch

import pytest

from agent.tools.url_guard import UnsafeUrlError, _is_blocked_address, assert_public_url


# --- schemes ---------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "file:///home/tektonix/.env",
    "data:text/html,<script>x</script>",
    "gopher://internal/",
    "ftp://host/f",
    "view-source:http://127.0.0.1/",
    "about:config",
    "//no-scheme.example/",
])
async def test_non_http_schemes_are_refused(url):
    with pytest.raises(UnsafeUrlError):
        await assert_public_url(url)


# --- literal internal addresses --------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:4101/",          # the review control port
    "http://127.0.0.1:8100/api/tasks",  # this agent's own API
    "http://localhost:3000/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.5.4/",
    "http://[::1]:8100/",
    "http://0.0.0.0/",
])
async def test_internal_addresses_are_refused(url):
    with pytest.raises(UnsafeUrlError):
        await assert_public_url(url)


# --- DNS rebinding ---------------------------------------------------------

async def test_public_hostname_resolving_to_loopback_is_refused():
    """The check that makes the whole thing work. Validating the hostname
    STRING is useless -- anyone can point evil.example at 127.0.0.1, so the
    guard must resolve first and judge the address."""
    fake = [(2, 1, 6, "", ("127.0.0.1", 80))]
    with patch("socket.getaddrinfo", return_value=fake):
        with pytest.raises(UnsafeUrlError, match="non-public"):
            await assert_public_url("http://totally-legit.example/")


async def test_mixed_public_and_private_records_are_refused():
    """A name with both a public and a private A record must not be a coin
    flip at connect time."""
    fake = [
        (2, 1, 6, "", ("93.184.216.34", 80)),
        (2, 1, 6, "", ("10.0.0.7", 80)),
    ]
    with patch("socket.getaddrinfo", return_value=fake):
        with pytest.raises(UnsafeUrlError):
            await assert_public_url("http://half-evil.example/")


async def test_public_hostname_is_allowed():
    fake = [(2, 1, 6, "", ("93.184.216.34", 443))]
    with patch("socket.getaddrinfo", return_value=fake):
        assert await assert_public_url("https://example.com/") == "https://example.com/"


async def test_unresolvable_host_is_refused_not_fetched():
    import socket as _s
    with patch("socket.getaddrinfo", side_effect=_s.gaierror("nope")):
        with pytest.raises(UnsafeUrlError):
            await assert_public_url("https://does-not-exist.invalid/")


# --- address classification ------------------------------------------------

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "10.1.2.3", "192.168.0.5", "172.20.0.1", "169.254.169.254",
    "0.0.0.0", "::1", "fc00::1", "fe80::1", "224.0.0.1", "not-an-ip",
])
def test_blocked_addresses(addr):
    assert _is_blocked_address(addr) is True


@pytest.mark.parametrize("addr", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_allowed(addr):
    assert _is_blocked_address(addr) is False


def test_ipv4_mapped_ipv6_loopback_is_refused():
    """::ffff:127.0.0.1 is loopback wearing an IPv6 hat."""
    assert _is_blocked_address("::ffff:127.0.0.1") is True


def test_every_blocked_category_is_actually_non_global():
    """Guards the allow-list direction: the guard permits only is_global, so
    a category nobody enumerated still fails closed."""
    for addr in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "fc00::1"):
        assert not ipaddress.ip_address(addr).is_global
