"""The agent can SHOW the operator an image, not only look at one.

2026-09-23: asked to "show me examples of the logo before we build anything",
a planning session rendered sixteen versions, read sixteen descriptions of
them, approved its own design, and the operator saw none. These pin the path
that puts pixels in front of a person -- and that it serves images and nothing
else, only to someone allowed to see the project.
"""
import base64
import dataclasses
import secrets
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import artifacts
from agent.auth import User
from agent.tools import show_tools

REPO = "test-repo"      # registered for every test by conftest


def _png(w=2, h=2) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setattr(artifacts, "ROOT", tmp_path / "artifacts")


# --- the store -------------------------------------------------------------

def test_an_image_is_stored_and_read_back_by_its_url():
    url = artifacts.save(REPO, _png(), caption="A")
    assert url.startswith(f"/api/artifacts/{REPO}/")
    data, ctype = artifacts.load(REPO, url.rsplit("/", 1)[1])
    assert data == _png() and ctype == "image/png"


@pytest.mark.parametrize("data", [
    b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    b"<html><script>alert(1)</script></html>",
    b"not an image at all",
])
def test_only_raster_images_by_their_own_bytes_are_accepted(data):
    """An SVG or HTML served from this origin is a document that runs script."""
    with pytest.raises(artifacts.ArtifactError):
        artifacts.save(REPO, data)


def test_an_unknown_project_or_a_malformed_id_reads_nothing():
    with pytest.raises(artifacts.ArtifactError):
        artifacts.save("no-such-project", _png())
    assert artifacts.load(REPO, "../../etc/passwd") is None
    assert artifacts.load("no-such-project", "0" * 32) is None


# --- the route -------------------------------------------------------------

def _user(repos):
    return User(id=2, email="u@example.com", role="user", allowed_repos=repos, totp_enabled=True,
                must_change_password=False, auto_approve_commands=False, require_merge_review=True)


@pytest.fixture
def client(monkeypatch):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    fake = dataclasses.replace(srv.config, auth_secret_key=key)
    monkeypatch.setattr(srv, "config", fake)
    monkeypatch.setattr(srv.app.state, "config", fake)
    return TestClient(srv.app)


def test_the_image_is_served_as_an_image_and_nothing_more(client, monkeypatch):
    url = artifacts.save(REPO, _png())
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _user([REPO]))
    r = client.get(url)
    assert r.status_code == 200 and r.content == _png()
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


def test_someone_without_the_project_cannot_see_its_images(client, monkeypatch):
    url = artifacts.save(REPO, _png())
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _user(["other"]))
    assert client.get(url).status_code == 403


def test_nobody_logged_out_sees_anything(client):
    url = artifacts.save(REPO, _png())
    assert client.get(url).status_code == 401


# --- the tool --------------------------------------------------------------

@pytest.fixture
def tool(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "public").mkdir(parents=True)
    (root / "public" / "logo.png").write_bytes(_png())
    rendered = []

    async def fake_render(svg, background, width, height):
        rendered.append((svg, background, width, height))
        return _png()

    monkeypatch.setattr(show_tools, "_render_svg", fake_render)
    return show_tools.make_show_images_tool(REPO, lambda: str(root)), rendered


async def test_svg_and_repo_images_come_back_as_images_in_the_chat(tool):
    tool, rendered = tool
    out = await tool.ainvoke({"images": [
        {"caption": "Current logo", "path": "public/logo.png"},
        {"caption": "A: traced and refined", "svg": "<svg/>", "background": "#1e1338"},
    ]})
    lines = [ln for ln in out.splitlines() if ln.startswith("![")]
    assert len(lines) == 2
    assert lines[0].startswith("![Current logo](/api/artifacts/test-repo/")
    assert rendered == [("<svg/>", "#1e1338", 800, 400)]
    assert "Repeat these lines in your reply" in out


async def test_a_path_outside_the_project_is_refused_and_the_rest_still_shown(tool):
    tool, _ = tool
    out = await tool.ainvoke({"images": [
        {"caption": "escape", "path": "../../etc/passwd"},
        {"caption": "fine", "svg": "<svg/>"},
    ]})
    assert out.count("![") == 1 and "Not shown" in out and "escape" in out


async def test_a_flood_of_images_is_refused(tool):
    tool, _ = tool
    out = await tool.ainvoke({"images": [{"caption": str(i), "svg": "<svg/>"} for i in range(9)]})
    assert out.startswith("ERROR")


def test_planning_and_build_both_have_it():
    """Logo work is decided in planning, by looking; the build shows what it made."""
    import inspect

    from agent import deep_agent, planning_chat
    assert "make_show_images_tool" in inspect.getsource(planning_chat)
    assert "*show_tools" in inspect.getsource(planning_chat)
    assert "make_show_images_tool" in inspect.getsource(deep_agent.build_deep_agent)
