"""The installable-app contract.

Chrome will not offer "Install app" unless a precise set of things is true at
once, and it reports the refusal only in DevTools -- so a manifest that lost
its 512px icon, or a service worker that stopped having a fetch handler, goes
out as "the install button disappeared and nobody knows when". Each check
below is one of Chrome's own criteria, or one of the two rules that keep the
worker safe on a live console.

Files, not a build: these all live in frontend/public/ and are copied to
dist/ verbatim, so the contract can be checked on a fresh clone with no npm.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
PUBLIC = FRONTEND / "public"
MANIFEST = PUBLIC / "manifest.webmanifest"
SW = PUBLIC / "sw.js"
INDEX = FRONTEND / "index.html"


def _png_size(path: Path) -> tuple[int, int]:
    """Width and height straight out of the IHDR chunk -- no Pillow, so this
    runs wherever pytest does."""
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", header[16:24])


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_the_manifest_has_what_chrome_requires(manifest):
    assert manifest["name"] and manifest["short_name"]
    # Anything but standalone/fullscreen and the install prompt never appears;
    # "browser" in particular is the default and silently disqualifies it.
    assert manifest["display"] in ("standalone", "fullscreen", "minimal-ui")
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"


def test_every_declared_icon_exists_at_the_size_it_claims(manifest):
    """A manifest is not validated against the disk: a renamed or resized icon
    is a silent 404 at install time, and Android falls back to a screenshot of
    the page as the launcher icon."""
    for icon in manifest["icons"]:
        path = PUBLIC / icon["src"].lstrip("/")
        assert path.is_file(), f"{icon['src']} is declared but not in frontend/public/"
        declared = tuple(int(n) for n in icon["sizes"].split("x"))
        assert _png_size(path) == declared, f"{icon['src']} is not {icon['sizes']}"


def test_both_icon_purposes_are_present(manifest):
    """192 and 512 `any` are Chrome's minimum. `maskable` is separate: without
    one, Android pads the `any` icon into a white circle, which is the
    "why is my app icon a grey blob" bug."""
    purposes = {(i["sizes"], i.get("purpose", "any")) for i in manifest["icons"]}
    assert ("192x192", "any") in purposes
    assert ("512x512", "any") in purposes
    assert ("512x512", "maskable") in purposes


def test_the_theme_colour_matches_the_app_background():
    """theme_color paints Android's status bar. If it drifts from the page's
    own background the bar reads as a wrong-coloured strip above the app."""
    manifest_colour = json.loads(MANIFEST.read_text())["theme_color"]
    head = INDEX.read_text()
    meta = re.search(r'<meta name="theme-color" content="([^"]+)"', head)
    assert meta, "index.html has no theme-color meta"
    assert meta.group(1).lower() == manifest_colour.lower()
    theme_css = (FRONTEND / "src" / "theme.css").read_text()
    bg = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", theme_css)
    assert bg, "theme.css has no --bg"
    assert manifest_colour.lower() == bg.group(1).lower(), (
        "theme_color drifted from --bg; the status bar will not match the page")


def test_the_head_links_the_manifest_and_the_ios_tags():
    """iOS ignores the manifest's display mode entirely and reads these
    instead, so an iPhone install falls back to a Safari-chrome window if any
    of them go missing."""
    head = INDEX.read_text()
    assert '<link rel="manifest" href="/manifest.webmanifest"' in head
    assert 'name="apple-mobile-web-app-capable" content="yes"' in head
    assert 'name="apple-mobile-web-app-title"' in head
    assert 'rel="apple-touch-icon"' in head
    # black-translucent only works because the viewport is already
    # viewport-fit=cover; without that pair the status bar sits on the header.
    assert 'content="black-translucent"' in head
    assert "viewport-fit=cover" in head


def test_the_worker_has_a_fetch_handler():
    """Chrome's installability check is specifically for a fetch handler; a
    worker that only precaches does not qualify."""
    assert SW.is_file()
    assert re.search(r"addEventListener\(\s*['\"]fetch['\"]", SW.read_text())


def test_the_worker_never_touches_the_api():
    """The rule the whole design rests on. /api carries the session cookie,
    every mutation, and the task/planning WebSocket upgrades; a worker that
    cached an authenticated response would put one account's data in a cache
    the next reader shares. The bypass is a bare `return` -- no respondWith --
    so the request goes to the network untouched."""
    src = SW.read_text()
    assert "isApi" in src, "the /api guard was renamed or removed"
    assert re.search(r"if \(isApi\(url\)\) return;", src), (
        "the /api bypass must return before any respondWith")
    # And the guard itself must still match the real prefix.
    assert "'/api/'" in src or '"/api/"' in src


def test_navigations_are_network_first():
    """Cache-first on a navigation is the white-screen bug: index.html names
    the hashed bundles of the deploy it came from, so a stale shell served
    while the network was fine asks for /assets/index-OLD.js, gets the SPA
    fallback's index.html back as JavaScript, and the app never boots."""
    src = SW.read_text()
    nav = src.split("request.mode === 'navigate'", 1)
    assert len(nav) == 2, "the navigation branch moved"
    body = nav[1][:900]
    fetch_at = body.find("fetch(request)")
    cache_at = body.find("caches.match")
    assert fetch_at != -1 and cache_at != -1, "navigation branch lost a path"
    assert fetch_at < cache_at, "the cache is consulted before the network on navigations"


def test_the_public_files_are_the_ones_the_build_copies():
    """vite copies public/ to dist/ verbatim; nothing else puts these there,
    so a file that moved out of public/ stops being served at the root."""
    for name in ("manifest.webmanifest", "sw.js", "icon-192.png",
                 "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        assert (PUBLIC / name).is_file(), f"frontend/public/{name} is missing"
