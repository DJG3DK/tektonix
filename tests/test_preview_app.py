"""Starting the project and looking at it.

A frontend task used to be done blind: edit the CSS, run the tests, and
neither the agent nor the reviewer ever saw the page. What this has to get
right is not the rendering -- that is Playwright's job -- but the two things
around it: the container always stops, and the browser's SSRF guard does not
get quietly disabled on the way.
"""
from __future__ import annotations

import asyncio

import pytest

from agent.tools import preview, sandbox
from agent.tools.url_guard import _same_origin


@pytest.mark.parametrize("url,origin,expected", [
    ("http://127.0.0.1:8080/", "http://127.0.0.1:8080", True),
    ("http://127.0.0.1:8080/nested/page?q=1", "http://127.0.0.1:8080", True),
    # The prefix trick this replaced a startswith() to stop.
    ("http://127.0.0.1:8080.evil.test/", "http://127.0.0.1:8080", False),
    ("http://127.0.0.1:4101/projects", "http://127.0.0.1:8080", False),
    ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8080", False),
    ("https://127.0.0.1:8080/", "http://127.0.0.1:8080", False),
])
def test_only_the_one_origin_this_process_allocated_is_allowed(url, origin, expected):
    """The model picks the command and the path. It never picks the host, so
    'preview my app' cannot become a way to reach the review control port or a
    cloud metadata endpoint."""
    assert _same_origin(url, origin) is expected


def test_a_port_that_is_not_a_port_is_refused_before_docker_runs():
    r = asyncio.run(sandbox.start_preview_container("sleep 1", "/tmp", 0))
    assert r["ok"] is False and "port number" in r["error"]
    r = asyncio.run(sandbox.start_preview_container("sleep 1", "/tmp", 99999))
    assert r["ok"] is False


def test_an_empty_command_is_refused_before_anything_starts(monkeypatch):
    started = []
    monkeypatch.setattr(sandbox, "start_preview_container",
                        lambda *a, **k: started.append(a) or {"ok": True})
    tool = preview.make_preview_tool(lambda: "/tmp")
    out = asyncio.run(tool.ainvoke({"command": "   ", "port": 3000}))
    assert "ERROR" in out
    assert started == [], "nothing was started"


def test_the_container_is_stopped_even_when_the_browser_raises(monkeypatch):
    """A dev server left running on a published port outlives the task that
    started it, and the next one gets a port conflict from a process nobody
    remembers."""
    stopped = []

    async def fake_start(cmd, cwd, port, env=None):
        return {"ok": True, "container": "c1", "port": 51234, "url": "http://127.0.0.1:51234"}

    async def fake_wait(*a, **k):
        return {"ok": True}

    async def boom(*a, **k):
        raise RuntimeError("playwright fell over")

    async def fake_stop(name):
        stopped.append(name)

    monkeypatch.setattr(preview, "start_preview_container", fake_start)
    monkeypatch.setattr(preview, "wait_for_preview", fake_wait)
    monkeypatch.setattr(preview, "stop_preview_container", fake_stop)
    import agent.tools.planning_tools as pt
    monkeypatch.setattr(pt, "run_browse_page_on_origin", boom)

    tool = preview.make_preview_tool(lambda: "/tmp")
    out = asyncio.run(tool.ainvoke({"command": "npm run dev", "port": 3000}))
    assert "playwright fell over" in out or "ERROR" in out
    assert stopped == ["c1"], "the container was left running"


def test_an_app_that_never_serves_returns_its_own_output(monkeypatch):
    """The useful thing when a dev server dies on a syntax error is the error,
    not 'timed out'."""
    stopped = []

    async def fake_start(cmd, cwd, port, env=None):
        return {"ok": True, "container": "c1", "port": 51234, "url": "http://127.0.0.1:51234"}

    async def fake_wait(*a, **k):
        return {"ok": False, "error": "the app exited before it served anything",
                "logs": "SyntaxError: Unexpected token '<'"}

    monkeypatch.setattr(preview, "start_preview_container", fake_start)
    monkeypatch.setattr(preview, "wait_for_preview", fake_wait)
    monkeypatch.setattr(preview, "stop_preview_container",
                        lambda n: asyncio.sleep(0, result=stopped.append(n)))

    tool = preview.make_preview_tool(lambda: "/tmp")
    out = asyncio.run(tool.ainvoke({"command": "npm run dev", "port": 3000}))
    assert "SyntaxError" in out
    assert stopped == ["c1"]


def test_the_preview_port_is_published_to_loopback_only():
    """Published anywhere else and a half-finished app is on the internet."""
    import inspect
    src = inspect.getsource(sandbox.start_preview_container)
    assert '"-p", f"127.0.0.1:{host_port}:{container_port}"' in src
