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


async def test_rendering_stops_until_the_operator_has_been_shown_something(monkeypatch, tmp_path):
    """A planner rendered one logo twenty times, asking the vision model a
    question of taste and tweaking on each hedged answer, and showed the
    operator nothing. A few renders check the work; past that it is a
    decision, and the decision is theirs."""
    import base64 as _b64

    from agent.tools import logo_tools

    calls = []

    async def fake_call(op, args):
        calls.append(op)
        return {"success": True, "pngBase64": _b64.b64encode(_png()).decode()}

    async def fake_describe(data, mime, prompt):
        return "a purple gem"

    monkeypatch.setattr(logo_tools, "installed", lambda: True)
    monkeypatch.setattr(logo_tools, "_call", fake_call)
    monkeypatch.setattr("agent.tools.vision.describe_image_bytes", fake_describe)
    state = logo_tools.new_show_state()
    render = {t.name: t for t in logo_tools.make_logo_tools(lambda: str(tmp_path), show_state=state)}["logo_render"]
    show = show_tools.make_show_images_tool(REPO, lambda: str(tmp_path), show_state=state)

    for _ in range(logo_tools.RENDERS_BEFORE_SHOWING):
        assert "purple gem" in await render.ainvoke({"svg": "<svg/>"})
    refused = await render.ainvoke({"svg": "<svg/>"})
    assert refused.startswith("STOP RENDERING") and "show_images" in refused
    assert len(calls) == logo_tools.RENDERS_BEFORE_SHOWING, "a refused render never reaches the renderer"

    shown = await show.ainvoke({"images": [{"caption": "current", "svg": "<svg/>"}]})
    assert shown.startswith("![current]")
    assert "purple gem" in await render.ainvoke({"svg": "<svg/>"}), "showing the operator resets it"


# --- drafts: large SVGs travel by name ---------------------------------------

BIG_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
           + "".join(f'<rect x="{i % 100}" y="{i % 50}" width="1" height="1" fill="#7c3aed"/>' for i in range(400))
           + "</svg>")


async def test_a_large_svg_comes_back_as_a_draft_and_every_tool_takes_one(monkeypatch, tmp_path):
    """A 95 KB trace, passed around as markup, meant the model retyping it --
    minutes of output, past the per-call timeout."""
    import base64 as _b64
    import re

    from agent import artifacts as arts
    from agent.tools import logo_tools

    rendered = []

    async def fake_call(op, args):
        if op == "optimize_svg":
            return {"success": True, "svg": args["svg"], "originalSize": 1, "optimizedSize": 1,
                    "savedPercent": "0%"}
        rendered.append(args["svg"])
        return {"success": True, "pngBase64": _b64.b64encode(_png()).decode()}

    async def fake_describe(data, mime, prompt):
        return "a purple mark"

    monkeypatch.setattr(logo_tools, "installed", lambda: True)
    monkeypatch.setattr(logo_tools, "_call", fake_call)
    monkeypatch.setattr("agent.tools.vision.describe_image_bytes", fake_describe)
    tools = {t.name: t for t in logo_tools.make_logo_tools(lambda: str(tmp_path), repo=REPO)}

    out = await tools["logo_optimize_svg"].ainvoke({"svg": BIG_SVG})
    assert "<svg" not in out, "a large result is not handed back as markup"
    draft = re.search(r"draft ([0-9a-f]{12})", out).group(1)
    assert arts.load_draft(REPO, draft) == BIG_SVG

    assert "purple mark" in await tools["logo_render"].ainvoke({"draft": draft})
    assert rendered[-1] == BIG_SVG

    composed = await tools["logo_compose"].ainvoke({
        "draft": draft, "defs": '<filter id="glow"><feGaussianBlur stdDeviation="3"/></filter>',
        "group_attributes": 'filter="url(#glow)"', "after": '<circle cx="80" cy="10" r="3" fill="#fff"/>'})
    new = re.search(r"draft ([0-9a-f]{12})", composed.split("onto draft")[1]).group(1)
    lit = arts.load_draft(REPO, new)
    assert '<filter id="glow">' in lit and '<g filter="url(#glow)">' in lit and lit.endswith('fill="#fff"/></svg>')
    assert arts.load_draft(REPO, draft) == BIG_SVG, "the original draft is untouched"

    shown = await show_tools.make_show_images_tool(REPO, lambda: str(tmp_path)).ainvoke(
        {"images": [{"caption": "original", "draft": draft}, {"caption": "with glow", "draft": new}]})
    assert shown.count("![") == 2
    assert "no draft" in await tools["logo_render"].ainvoke({"draft": "0" * 12})


def test_a_traced_logo_loses_its_white_canvas_and_nothing_else():
    """A logo on a white PNG traced to a white box: it could not sit on a dark
    header, and a glow behind it was hidden."""
    from agent.tools.logo_tools import _drop_traced_background

    traced = ('<svg xmlns="http://www.w3.org/2000/svg" width="587" height="230">'
              '<path d="M0,0C195,0,391,0,587,0L587,230L0,230Z" fill="#FDFDFD" transform="translate(0,0)"/>'
              '<path d="M101,82c-1,5-2,10-4,15Z" fill="#272727"/></svg>')
    out, removed = _drop_traced_background(traced)
    assert removed == "#FDFDFD" and "#FDFDFD" not in out and 'fill="#272727"' in out
    dark_canvas = traced.replace("#FDFDFD", "#1E1338")
    assert _drop_traced_background(dark_canvas) == (dark_canvas, None), "only a LIGHT canvas is taken"
    not_first = traced.replace('<path d="M101', '<path d="M5,5L6,6Z" fill="#000000"/><path d="M101')
    moved = not_first.replace('<path d="M0,0', '<path d="M5,5L7,7Z" fill="#111111"/><path d="M0,0', 1)
    assert _drop_traced_background(moved)[1] is None, "a background that is not the first shape is left alone"
