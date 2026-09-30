"""agent/tools/browse_guard.py, and the guard on the planning agent's
browse_page. 2026-09-30: twenty invented documentation URLs on one site,
nearly all "Page Not Found", and nothing counted them."""

import pytest

from agent.tools import planning_tools
from agent.tools.browse_guard import BrowseGuard, looks_missing

NOT_FOUND = "# SimpleWebAuthn\nURL: https://simplewebauthn.dev/docs/x\n\nSkip to main content … Page Not Found We could not find what you were looking for."
REAL = "# Introduction | SimpleWebAuthn\nURL: https://simplewebauthn.dev/docs\n\nSimpleWebAuthn is a collection of libraries …"


def test_error_pages_are_recognised_and_real_pages_are_not():
    assert looks_missing(NOT_FOUND)
    assert looks_missing("ERROR: HTTP 404 -- this page does not exist or refused the request.\n# x")
    assert looks_missing("# 404 | Example\nURL: https://e.x/y\n\n…")
    assert not looks_missing(REAL)
    assert not looks_missing("# Handling a 500 error in Express\nURL: https://e.x/y\n\n" + "a" * 500 + " not found later on")


def test_three_missing_pages_close_that_site_but_not_others():
    g = BrowseGuard()
    for i in range(3):
        assert g.check(f"https://simplewebauthn.dev/docs/guess-{i}") is None
        g.record(f"https://simplewebauthn.dev/docs/guess-{i}", NOT_FOUND)
    refusal = g.check("https://simplewebauthn.dev/docs/guess-4")
    assert refusal and "Stop guessing addresses" in refusal and "simplewebauthn.dev" in refusal
    assert g.check("https://github.com/MasterKale/SimpleWebAuthn") is None, "another site is still open"


def test_real_pages_do_not_count_as_misses_but_do_count_toward_the_budget():
    g = BrowseGuard(budget=4)
    for i in range(4):
        assert g.check(f"https://docs.example.com/p{i}") is None
        g.record(f"https://docs.example.com/p{i}", REAL)
    assert g.misses == {}
    refusal = g.check("https://docs.example.com/p5")
    assert refusal and "the limit" in refusal


@pytest.mark.asyncio
async def test_the_planning_agents_browse_page_is_guarded(monkeypatch):
    calls = []

    async def fake_browse(url, screenshot, question, allow_origin=None):
        calls.append(url)
        return NOT_FOUND

    monkeypatch.setattr(planning_tools, "_run_browse_page", fake_browse)
    tools, _ = planning_tools.make_planning_tools()
    browse = {t.name: t for t in tools}["browse_page"]
    for i in range(5):
        out = await browse.ainvoke({"url": f"https://simplewebauthn.dev/docs/guess-{i}"})
    assert len(calls) == 3, "the fourth and fifth guesses never load"
    assert "Stop guessing addresses" in out


@pytest.mark.asyncio
async def test_a_new_planning_turn_starts_with_a_fresh_guard(monkeypatch):
    async def fake_browse(url, screenshot, question, allow_origin=None):
        return NOT_FOUND

    monkeypatch.setattr(planning_tools, "_run_browse_page", fake_browse)
    for _turn in range(2):
        tools, _ = planning_tools.make_planning_tools()
        browse = {t.name: t for t in tools}["browse_page"]
        out = await browse.ainvoke({"url": "https://simplewebauthn.dev/docs/one"})
        assert "Stop guessing" not in out


@pytest.mark.asyncio
async def test_the_coders_browse_page_is_guarded_too(monkeypatch):
    calls = []

    async def fake_browse(url, screenshot, question, allow_origin=None):
        calls.append(url)
        return NOT_FOUND

    monkeypatch.setattr(planning_tools, "_run_browse_page", fake_browse)
    browse = planning_tools.make_browse_page_tool()
    for i in range(4):
        out = await browse.ainvoke({"url": f"https://docs.example.com/guess-{i}"})
    assert len(calls) == 3 and "Stop guessing addresses" in out


class _FakePage:
    url = "https://simplewebauthn.dev/docs"

    async def eval_on_selector_all(self, selector, script):
        return [
            ["Server", "https://simplewebauthn.dev/docs/packages/server"],
            ["Server", "https://simplewebauthn.dev/docs/packages/server#top"],
            ["GitHub", "https://github.com/MasterKale/SimpleWebAuthn"],
            ["", "mailto:someone@example.com"],
            ["Here", "https://simplewebauthn.dev/docs"],
            ["Browser", "https://simplewebauthn.dev/docs/packages/browser"],
        ]


@pytest.mark.asyncio
async def test_a_page_lists_its_real_links_same_site_first():
    links = await planning_tools._page_links(_FakePage())
    assert links == [
        "- Server -> https://simplewebauthn.dev/docs/packages/server",
        "- Browser -> https://simplewebauthn.dev/docs/packages/browser",
        "- GitHub -> https://github.com/MasterKale/SimpleWebAuthn",
    ]
