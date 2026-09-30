"""agent/tools/web_search.py: search through the router, DuckDuckGo as a
fallback, and "unavailable" instead of a false "no results".

2026-09-30: Bing scraped from this box answered "no results" for
"simplewebauthn", and a planning turn then guessed twenty documentation URLs.
"""

import pytest

from agent.tools import web_search as ws

ROUTER_PAYLOAD = {
    "choices": [{"message": {"content": "…", "annotations": [
        {"type": "url_citation", "url_citation": {"url": "https://www.npmjs.com/package/@simplewebauthn/server",
                                                   "title": "@simplewebauthn/server", "content": "SimpleWebAuthn for Servers " * 40}},
        {"type": "url_citation", "url_citation": {"url": "https://www.npmjs.com/package/@simplewebauthn/server",
                                                   "title": "duplicate", "content": ""}},
        {"type": "url_citation", "url_citation": {"url": "javascript:alert(1)", "title": "bad", "content": ""}},
        {"type": "url_citation", "url_citation": {"url": "https://simplewebauthn.dev/docs/packages/server",
                                                   "title": "server docs", "content": "generateRegistrationOptions"}},
    ]}}],
}

DDG_PAGE = """
<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsimplewebauthn.dev%2F&amp;rut=x">Simple<b>WebAuthn</b></a>
<a class="result__snippet" href="#">A collection of <b>TypeScript</b> libraries</a></div>
<div class="result"><a rel="nofollow" class="result__a" href="https://github.com/MasterKale/SimpleWebAuthn">GitHub - MasterKale/SimpleWebAuthn</a></div>
"""


def test_citations_become_results_deduplicated_and_http_only():
    got = ws.parse_citations(ROUTER_PAYLOAD, 10)
    assert [r.url for r in got] == ["https://www.npmjs.com/package/@simplewebauthn/server",
                                    "https://simplewebauthn.dev/docs/packages/server"]
    assert ws.parse_citations(ROUTER_PAYLOAD, 1)[0].title == "@simplewebauthn/server"


def test_the_formatted_text_trims_snippets_and_numbers_results():
    text = ws.format_results("q", ws.parse_citations(ROUTER_PAYLOAD, 10), "web")
    assert text.startswith("1. @simplewebauthn/server\n   https://www.npmjs.com/")
    assert "\n\n2. server docs" in text
    assert "…" in text, "a long snippet is cut"


def test_duckduckgo_results_are_unwrapped():
    got = ws.parse_ddg(DDG_PAGE, 10)
    assert [(r.title, r.url) for r in got] == [
        ("SimpleWebAuthn", "https://simplewebauthn.dev/"),
        ("GitHub - MasterKale/SimpleWebAuthn", "https://github.com/MasterKale/SimpleWebAuthn"),
    ]
    assert got[0].snippet == "A collection of TypeScript libraries"


@pytest.mark.asyncio
async def test_the_router_is_asked_first_and_its_results_used(monkeypatch):
    async def router(query, n, metadata):
        assert n == 3
        return ws.parse_citations(ROUTER_PAYLOAD, n)

    async def ddg(query, n):
        raise AssertionError("the fallback must not run when the router answered")

    monkeypatch.setattr(ws, "_via_router", router)
    monkeypatch.setattr(ws, "_via_duckduckgo", ddg)
    out = await ws.web_search("simplewebauthn server npm", 3)
    assert "npmjs.com/package/@simplewebauthn/server" in out


@pytest.mark.asyncio
async def test_duckduckgo_answers_when_the_router_cannot(monkeypatch):
    async def router(query, n, metadata):
        raise RuntimeError("router answered 502")

    async def ddg(query, n):
        return ws.parse_ddg(DDG_PAGE, n)

    monkeypatch.setattr(ws, "_via_router", router)
    monkeypatch.setattr(ws, "_via_duckduckgo", ddg)
    out = await ws.web_search("simplewebauthn", 5)
    assert out.startswith("1. SimpleWebAuthn\n   https://simplewebauthn.dev/")


@pytest.mark.asyncio
async def test_when_every_source_fails_the_model_is_told_not_to_guess(monkeypatch):
    async def fail(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(ws, "_via_router", fail)
    monkeypatch.setattr(ws, "_via_duckduckgo", fail)
    out = await ws.web_search("simplewebauthn", 5)
    assert out.startswith("Web search is unavailable right now")
    assert "Do not guess page addresses" in out
    assert "No results" not in out, "an outage is never reported as an empty answer"


@pytest.mark.asyncio
async def test_the_router_request_names_the_alias_and_the_web_plugin(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return ROUTER_PAYLOAD

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen.update(url=url, body=json, auth=headers["Authorization"])
            return _Resp()

    monkeypatch.setenv("MODEL_ROUTER_URL", "http://router.test/v1/")
    monkeypatch.setenv("MODEL_ROUTER_KEY", "sk-test")
    monkeypatch.setattr(ws.httpx, "AsyncClient", _Client)
    got = await ws._via_router("q", 4, {"agent_task_id": "t1"})
    assert seen["url"] == "http://router.test/v1/chat/completions" and seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "web-search"
    assert seen["body"]["plugins"] == [{"id": "web", "max_results": 4}]
    assert seen["body"]["metadata"] == {"agent_task_id": "t1"}
    assert len(got) == 2


def test_the_router_config_has_the_web_search_alias():
    import yaml
    from agent import paths
    cfg = yaml.safe_load((paths.REPO_ROOT / "services/model-router/config.example.yaml").read_text())
    assert "web-search" in [m["model_name"] for m in cfg["model_list"]]
