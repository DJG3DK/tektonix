r"""The logo tools, and the two things upstream gets wrong for a model caller.

LogoLoom (MIT, mcpware/logoloom) is sound for its intended caller -- a person
in an editor. Ours is a model, whose arguments can come from a repo file or a
web page it read, and that changes two of its assumptions:

  * image-to-svg.mjs interpolates the image path into a shell string
    (`execSync(\`vtracer --input ${imagePath} ...\`)`). A model-chosen path is
    live command injection, so the path handed to it is never the model's.
  * the same function falls back to `npx -y vtracer-cli`, which is a network
    fetch of an unpinned package, in the middle of a task.

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


@needs_logoloom
def test_colour_tracing_says_what_is_missing_rather_than_fetching_it(tmp_path, monkeypatch):
    """Upstream's fallback is `npx -y vtracer-cli` -- a network fetch of an
    unpinned package mid-task. The bridge checks first and says so."""
    src = tmp_path / "logo.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    r = _bridge("image_to_svg", {"imagePath": str(src), "colorMode": "color"})
    if r.get("success") is False and "vtracer" in (r.get("error") or ""):
        assert "not installed" in r["error"] or "Install vtracer" in r["error"]


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
