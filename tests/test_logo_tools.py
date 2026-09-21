r"""The logo tools, and the two things upstream gets wrong for a model caller.

LogoLoom (MIT, mcpware/logoloom) is sound for its intended caller -- a person
in an editor. Ours is a model, whose arguments can come from a repo file or a
web page it read, and that changes two of its assumptions:

  * image-to-svg.mjs interpolates the image path into a shell string
    (`execSync(\`vtracer --input ${imagePath} ...\`)`). A model-chosen path is
    live command injection. The bridge traces with an argv array instead, so
    no shell is involved at all, and still copies the file to a name it
    generated so nothing the model chose reaches a subprocess.
  * the same function passes vtracer 0.6's flag names (--colormode,
    --filter_speckle) to a CLI that renamed them, so every call fails
    whatever the path -- which is why the bridge invokes vtracer itself.

Neither is a criticism of the package. They are the consequences of putting a
model in front of it, and the bridge is where they are dealt with.

The rest here pins the boring, important parts: writes stay inside the
project, a seat that must not write cannot export, and a missing install
produces no tools rather than tools that always fail.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.tools import logo_tools as lt

BRIDGE = lt.BRIDGE
needs_logoloom = pytest.mark.skipif(not lt.installed(),
                                    reason="LogoLoom not installed (services/logoloom)")

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40">'
       '<rect width="120" height="40" fill="#0b1020"/>'
       '<circle cx="20" cy="20" r="12" fill="#4ade80"/>'
       '<text x="40" y="27" font-size="16" fill="#fff">Ten</text></svg>')


def _bridge(op: str, args: dict) -> dict:
    out = subprocess.run(["node", str(BRIDGE), json.dumps({"op": op, "args": args})],
                         cwd=str(BRIDGE.parent), capture_output=True, text=True, timeout=180)
    return json.loads(out.stdout or "{}")


# --- the injection path ---------------------------------------------------

@needs_logoloom
def test_a_filename_that_is_a_shell_payload_does_not_run(tmp_path):
    """The one that matters. Upstream interpolates this straight into a shell
    string; the bridge copies the file to a name it generated, so the shell
    only ever sees that.

    The payload carries no '/' -- with one it would just be a path that does
    not exist, and the test would pass without exercising anything. So it
    lands in the bridge's own working directory, which is where a shell
    running it would put it.
    """
    canary = BRIDGE.parent / "PWNED-injection-canary"
    canary.unlink(missing_ok=True)
    evil = tmp_path / "a;touch PWNED-injection-canary;b.png"
    evil.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    assert evil.is_file(), "the file whose NAME is the payload must really exist"

    try:
        _bridge("image_to_svg", {"imagePath": str(evil), "colorMode": "binary"})
        assert not canary.exists(), "the filename was executed as a shell command"
    finally:
        canary.unlink(missing_ok=True)


@needs_logoloom
def test_a_missing_path_is_reported_before_any_shell_runs(tmp_path):
    r = _bridge("image_to_svg", {"imagePath": str(tmp_path / "x; id > /tmp/nope.txt"),
                                 "colorMode": "binary"})
    assert r["success"] is False and "no such file" in r["error"].lower()
    assert not Path("/tmp/nope.txt").exists()


def test_nothing_here_can_reach_for_the_network():
    """Upstream's vectorizer fallback is `npx -y vtracer-cli` -- a fetch of an
    unpinned package, mid-task, on a box that may have no egress. The bridge
    does not invoke it, and the reason this is asserted against the source is
    that the branch is unreachable once vtracer IS installed, which is the
    state a passing test suite runs in."""
    src = BRIDGE.read_text()
    assert "npx" not in src
    assert "vtracer-cli" not in src
    assert "no vectorizer installed" in src, "the honest message must still be there"


@pytest.mark.skipif(not lt.installed(), reason="LogoLoom not installed")
def test_the_vectorizer_is_found_in_vendor_not_on_the_system_path():
    """It is installed beside bridge.mjs rather than in /usr/local/bin, so
    that removing the logo tools is deleting a directory rather than
    archaeology. This only means anything while the system PATH has no
    vtracer of its own -- which is the case here, and is the point."""
    if shutil.which("vtracer"):
        pytest.skip("this box has a system vtracer, so the distinction is untestable")
    vendored = BRIDGE.parent / "vendor" / "vtracer"
    if not vendored.is_file():
        pytest.skip("vtracer not fetched (services/logoloom/fetch-vtracer.sh)")
    assert "vendor" in BRIDGE.read_text(), "bridge.mjs must put vendor/ on its own PATH"


_HAS_VTRACER = (BRIDGE.parent / "vendor" / "vtracer").is_file()
needs_vtracer = pytest.mark.skipif(
    not (lt.installed() and _HAS_VTRACER),
    reason="vtracer not fetched (services/logoloom/fetch-vtracer.sh)")


def _png(tmp_path: Path, name: str = "logo.png") -> Path:
    """A flat three-colour mark, which is what tracing is actually for."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
           '<rect width="200" height="200" fill="#ffffff"/>'
           '<circle cx="100" cy="100" r="70" fill="#4ade80"/>'
           '<path d="M70 100 L100 60 L130 100 L100 140 Z" fill="#0b1020"/></svg>')
    import base64
    data = _bridge("render_png", {"svg": svg, "width": 400, "height": 400,
                                  "background": "#ffffff"})["pngBase64"]
    out = tmp_path / name
    out.write_bytes(base64.b64decode(data))
    return out


@needs_vtracer
def test_colour_tracing_recovers_the_colours(tmp_path):
    """Upstream's own invocation cannot get this far: it passes --colormode
    and --filter_speckle, which vtracer 1.0 renamed, so it fails on every
    input. The bridge calls vtracer itself with the current flags."""
    _png(tmp_path)
    tools = {t.name: t for t in lt.make_logo_tools(lambda: str(tmp_path))}
    out = asyncio.run(tools["logo_trace_image"].ainvoke({"image_path": "logo.png"}))
    assert not out.startswith("ERROR"), out
    svg = out.split("\n\n", 1)[1]
    assert svg.count("<path") >= 2, "a three-colour mark should not trace to one path"
    fills = {m.lower() for m in re.findall(r'fill="(#[0-9a-fA-F]{6})"', svg)}
    assert any(f.startswith("#4a") for f in fills), f"the green is gone: {fills}"
    assert any(f.startswith("#0b") for f in fills), f"the navy is gone: {fills}"


@needs_vtracer
def test_binary_tracing_gives_one_silhouette(tmp_path):
    _png(tmp_path)
    tools = {t.name: t for t in lt.make_logo_tools(lambda: str(tmp_path))}
    out = asyncio.run(tools["logo_trace_image"].ainvoke(
        {"image_path": "logo.png", "color_mode": "binary"}))
    assert not out.startswith("ERROR"), out
    svg = out.split("\n\n", 1)[1]
    assert "<path" in svg


@needs_vtracer
def test_tracing_a_file_whose_name_is_a_payload_still_traces_it(tmp_path):
    """The defence must not be "refuse anything odd" -- a file with a
    semicolon in its name is a legal file, and it should trace."""
    canary = BRIDGE.parent / "PWNED-trace-canary"
    canary.unlink(missing_ok=True)
    src = _png(tmp_path)
    evil = tmp_path / "a;touch PWNED-trace-canary;b.png"
    evil.write_bytes(src.read_bytes())
    tools = {t.name: t for t in lt.make_logo_tools(lambda: str(tmp_path))}
    try:
        out = asyncio.run(tools["logo_trace_image"].ainvoke({"image_path": evil.name}))
        assert not out.startswith("ERROR"), out
        assert not canary.exists(), "the filename was executed"
    finally:
        canary.unlink(missing_ok=True)


# --- what the tools refuse ------------------------------------------------

@needs_logoloom
def test_an_export_cannot_write_outside_the_project(tmp_path):
    tools = {t.name: t for t in lt.make_logo_tools(lambda: str(tmp_path))}
    for bad in ("../escape", "/etc/brand", "sub/../../out"):
        out = asyncio.run(tools["logo_export_brand_kit"].ainvoke(
            {"svg": SVG, "output_dir": bad, "name": "X"}))
        assert out.startswith("ERROR:") and "not a valid repo path" in out, bad
    assert not (tmp_path.parent / "escape").exists()


@needs_logoloom
def test_tracing_cannot_read_outside_the_project(tmp_path):
    tools = {t.name: t for t in lt.make_logo_tools(lambda: str(tmp_path))}
    out = asyncio.run(tools["logo_trace_image"].ainvoke({"image_path": "../../etc/hostname"}))
    assert out.startswith("ERROR:") and "not a valid repo path" in out


@needs_logoloom
@pytest.mark.parametrize("tool_name", ["logo_text_to_path", "logo_optimize_svg", "logo_render"])
def test_a_path_where_the_markup_goes_is_refused_without_a_subprocess(tmp_path, tool_name):
    """The common mistake, and upstream's answer to it is a stack trace."""
    tools = {t.name: t for t in lt.make_logo_tools(lambda: str(tmp_path))}
    out = asyncio.run(tools[tool_name].ainvoke({"svg": "brand/logo.svg"}))
    assert "does not contain an <svg>" in out


@needs_logoloom
def test_an_export_without_a_name_is_refused(tmp_path):
    tools = {t.name: t for t in lt.make_logo_tools(lambda: str(tmp_path))}
    out = asyncio.run(tools["logo_export_brand_kit"].ainvoke(
        {"svg": SVG, "output_dir": "brand", "name": "  "}))
    assert "give the brand name" in out


# --- which seat gets what -------------------------------------------------

def test_a_seat_that_must_not_write_has_no_export(tmp_path):
    """The planning chat mutates nothing by contract; a brand kit is two
    dozen binary files with nowhere to go in a conversation."""
    names = {t.name for t in lt.make_logo_tools(lambda: str(tmp_path), can_export=False)}
    assert "logo_export_brand_kit" not in names
    if names:
        assert "logo_render" in names, "it must still be able to look at a concept"


def test_no_install_means_no_tools(monkeypatch, tmp_path):
    """Better than four tools whose every call is an error about node_modules."""
    monkeypatch.setattr(lt, "BRIDGE", tmp_path / "nope" / "bridge.mjs")
    assert lt.installed() is False
    assert lt.make_logo_tools(lambda: str(tmp_path)) == []


# --- the pipeline ---------------------------------------------------------

@needs_logoloom
def test_the_whole_pipeline_produces_a_kit_in_the_project(tmp_path):
    """Outline the type, optimise, export -- the order the prompt teaches."""
    tools = {t.name: t for t in lt.make_logo_tools(lambda: str(tmp_path))}

    outlined = asyncio.run(tools["logo_text_to_path"].ainvoke({"svg": SVG}))
    assert "Converted 1" in outlined
    svg = outlined.split("\n\n", 1)[1]
    assert "<text" not in svg, "the type is still font-dependent"

    optimised = asyncio.run(tools["logo_optimize_svg"].ainvoke({"svg": svg}))
    assert "bytes" in optimised
    final = optimised.split("\n\n", 1)[1]

    out = asyncio.run(tools["logo_export_brand_kit"].ainvoke(
        {"svg": final, "output_dir": "brand", "name": "Ten", "primary": "#4ade80"}))
    assert not out.startswith("ERROR"), out
    written = sorted(os.listdir(tmp_path / "brand"))
    assert "BRAND.md" in written and "favicon.ico" in written
    assert any(f.endswith(".png") for f in written) and any(f.endswith(".svg") for f in written)
    assert len(written) >= 20, written
    # and the summary tells the operator what just landed in their repo
    assert "binary assets" in out and "brand/" in out
