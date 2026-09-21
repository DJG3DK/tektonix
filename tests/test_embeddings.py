"""The embedder: what it refuses to do, and what it must never pay for twice.

Most of this file is about absence. Semantic recall has three prerequisites
-- a switch, a router deployment, and somewhere for the database to put a
vector -- and the one thing that must not happen is an installation missing
any of them discovering it at write time, on the last step of a task that
has already succeeded. On Postgres the third is an extension the app role
cannot create for itself; on SQLite it is a pip wheel and nothing more.

The rest is about cost. An embedding per terminal task is small money
forever, which is the kind that goes unnoticed: identical text inside one
call is embedded once, a caller holding a digest can skip the call entirely,
and a batch is one request.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from agent import embeddings
from agent.config import load_config


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    embeddings.reset_probe_cache()
    yield
    embeddings.reset_probe_cache()


def _config(**over):
    base = dataclasses.replace(
        load_config(),
        embeddings_enabled=True,
        embedding_alias="embedder",
        embedding_dims=4,
        router_base_url="http://router.invalid/v1",
        router_api_key="sk-test",
    )
    return dataclasses.replace(base, **over)


def _responder(monkeypatch, handler):
    """Answer agent/embeddings.py's POSTs without a network."""
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=httpx.MockTransport(transport), **kw)

    monkeypatch.setattr(embeddings.httpx, "AsyncClient", factory)
    return seen


def _vectors(request: httpx.Request, _n: int, dims: int = 4) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    return httpx.Response(200, json={
        "object": "list", "model": "text-embedding-3-small", "provider": "OpenAI",
        "data": [{"index": i, "embedding": [float(i)] * dims} for i in range(len(inputs))],
        "usage": {"prompt_tokens": len(inputs), "cost": 2e-08},
    })


# ---------------------------------------------------------------------------
# availability: three ways to be absent, one of them a decision
# ---------------------------------------------------------------------------

def test_the_feature_being_off_costs_no_probe(monkeypatch):
    """Off is the default, so this runs on every install that never wanted
    the feature. It must not open a connection to say so."""
    def boom(*a, **k):
        raise AssertionError("a disabled capability must probe nothing")

    monkeypatch.setattr(embeddings.httpx, "get", boom)
    assert embeddings.available(_config(embeddings_enabled=False)) is False


def test_a_router_without_the_alias_is_unavailable(monkeypatch):
    """The Models page cannot show this alias and nothing else would notice
    it is missing -- so it is asked for by name before anything relies on it."""
    monkeypatch.setattr(embeddings, "_store_can_hold_vectors", lambda config: True)
    monkeypatch.setattr(embeddings.httpx, "get", lambda *a, **k: httpx.Response(
        200, json={"data": [{"id": "agent-coder"}]}, request=httpx.Request("GET", "http://x")))
    assert embeddings.available(_config()) is False


def test_an_unreachable_router_is_unavailable_not_an_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(embeddings, "_store_can_hold_vectors", lambda config: True)
    monkeypatch.setattr(embeddings.httpx, "get", boom)
    assert embeddings.available(_config()) is False


def test_a_database_without_pgvector_is_unavailable(monkeypatch):
    """The app role is not superuser and `vector` is not a trusted extension,
    so this state is reached by installing and forgetting one command. It has
    to read as a doctor line, never as a crash in the episode writer."""
    monkeypatch.setattr(embeddings, "_router_has_alias", lambda config: True)
    monkeypatch.setattr(embeddings, "_store_can_hold_vectors", lambda config: False)
    assert embeddings.available(_config()) is False


def test_everything_present_is_available(monkeypatch):
    monkeypatch.setattr(embeddings, "_router_serves_embeddings", lambda config: True)
    monkeypatch.setattr(embeddings, "_router_has_alias", lambda config: True)
    monkeypatch.setattr(embeddings, "_store_can_hold_vectors", lambda config: True)
    assert embeddings.available(_config()) is True


def test_the_answer_is_cached_per_process(monkeypatch):
    monkeypatch.setattr(embeddings, "_router_serves_embeddings", lambda config: True)
    calls = []
    monkeypatch.setattr(embeddings, "_router_has_alias", lambda config: calls.append(1) or True)
    monkeypatch.setattr(embeddings, "_store_can_hold_vectors", lambda config: True)
    config = _config()
    assert embeddings.available(config) and embeddings.available(config)
    assert len(calls) == 1, "a seat build asks this repeatedly; it must not probe every time"


def test_a_router_that_predates_the_endpoint_is_unavailable(monkeypatch):
    """The state this box was actually in: config.yaml already named
    `embedder`, so /v1/models listed it, while the running process answered
    404 to /v1/embeddings. Reported as available, every episode write would
    have made a doomed call -- caught and warned about, and invisible."""
    monkeypatch.setattr(embeddings, "_router_has_alias", lambda config: True)
    monkeypatch.setattr(embeddings, "_store_can_hold_vectors", lambda config: True)
    monkeypatch.setattr(
        embeddings.httpx, "post",
        lambda *a, **kw: httpx.Response(404, json={"detail": "Not Found"}))
    assert embeddings.available(_config()) is False


def test_the_route_existing_is_enough_even_when_the_call_is_refused(monkeypatch):
    """Probed with no model, which the route rejects before it spends
    anything. A 400 or a 401 both prove the route is there; only a 404 does
    not."""
    monkeypatch.setattr(embeddings, "_router_has_alias", lambda config: True)
    monkeypatch.setattr(embeddings, "_store_can_hold_vectors", lambda config: True)
    monkeypatch.setattr(
        embeddings.httpx, "post",
        lambda *a, **kw: httpx.Response(400, json={"detail": "no model given"}))
    assert embeddings.available(_config()) is True


def test_a_sqlite_installation_needs_nobody_to_install_anything():
    """The backend that can be proved anywhere. sqlite-vec is a wheel with
    the extension compiled in and it arrives with langgraph-checkpoint-sqlite,
    so unlike pgvector there is no apt, no superuser and no one-off command
    -- the only thing that can be missing is the CLI requirements file."""
    from agent.backends import sqlite_available

    answer = embeddings._store_can_hold_vectors(_config(dsn="sqlite:///tmp/state.db"))
    assert answer is sqlite_available() and embeddings._sqlite_can_load_extensions()


def test_a_sqlite_installation_without_the_cli_extra_says_so(monkeypatch):
    monkeypatch.setattr(embeddings, "sqlite_available", lambda: False)
    assert embeddings._store_can_hold_vectors(_config(dsn="sqlite:///tmp/state.db")) is False


def test_the_capability_is_listed_for_the_doctor():
    from agent.capabilities import CAPABILITIES

    capability = next((c for c in CAPABILITIES if c.name == "episode embeddings"), None)
    assert capability is not None
    assert "CREATE EXTENSION vector" in capability.hint, (
        "the person reading the hint is the person who has to run the command")


# ---------------------------------------------------------------------------
# the digest, which is what stops the same text being paid for twice
# ---------------------------------------------------------------------------

def test_the_digest_is_stable_and_text_dependent():
    assert embeddings.digest("a goal") == embeddings.digest("a goal")
    assert embeddings.digest("a goal") != embeddings.digest("a goal.")


def test_unchanged_text_is_recognised():
    text = "make the health endpoint check the database"
    assert embeddings.unchanged(text, embeddings.digest(text)) is True
    assert embeddings.unchanged(text + "!", embeddings.digest(text)) is False


def test_a_record_with_no_digest_has_to_be_embedded_once():
    """Written before digests existed. Claiming it is unchanged would leave it
    without a vector forever."""
    assert embeddings.unchanged("anything", None) is False


# ---------------------------------------------------------------------------
# embedding
# ---------------------------------------------------------------------------

async def test_no_texts_is_no_request(monkeypatch):
    seen = _responder(monkeypatch, _vectors)
    assert await embeddings.aembed([], _config()) == []
    assert seen == []


async def test_a_vector_per_text_in_order(monkeypatch):
    _responder(monkeypatch, _vectors)
    out = await embeddings.aembed(["a", "b", "c"], _config())
    assert out == [[0.0] * 4, [1.0] * 4, [2.0] * 4]


async def test_the_answer_is_ordered_by_index_not_by_arrival(monkeypatch):
    """The OpenAI shape carries an index and the whole contract of aembed is
    positional: langgraph pairs vector N with the item it put at position N."""
    def shuffled(request, _n):
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [1.0] * 4},
            {"index": 0, "embedding": [0.0] * 4},
        ]})

    _responder(monkeypatch, shuffled)
    assert await embeddings.aembed(["a", "b"], _config()) == [[0.0] * 4, [1.0] * 4]


async def test_repeated_text_is_embedded_once_and_handed_back_twice(monkeypatch):
    """A backfill page from one project repeats goal text far more often than
    prose would, and every repeat is a token that would be billed again."""
    seen = _responder(monkeypatch, _vectors)
    out = await embeddings.aembed(["same", "other", "same"], _config())
    assert json.loads(seen[0].content)["input"] == ["same", "other"]
    assert out[0] == out[2] and out[0] != out[1]


async def test_a_batch_is_one_request(monkeypatch):
    seen = _responder(monkeypatch, _vectors)
    texts = [f"episode {i}" for i in range(embeddings.MAX_BATCH)]
    assert len(await embeddings.aembed(texts, _config())) == len(texts)
    assert len(seen) == 1


async def test_more_than_one_batch_is_split(monkeypatch):
    seen = _responder(monkeypatch, _vectors)
    texts = [f"episode {i}" for i in range(embeddings.MAX_BATCH + 1)]
    out = await embeddings.aembed(texts, _config())
    assert len(out) == len(texts)
    assert [len(json.loads(r.content)["input"]) for r in seen] == [embeddings.MAX_BATCH, 1]


async def test_the_task_id_travels_as_routing_metadata(monkeypatch):
    """The router records it and does not forward it. Without it an embedding
    can be priced but never totalled into the task that caused it."""
    seen = _responder(monkeypatch, _vectors)
    await embeddings.aembed(["a"], _config(), task_id="T1", session_id="S1")
    body = json.loads(seen[0].content)
    assert body["metadata"] == {"agent_task_id": "T1", "agent_session_id": "S1"}
    assert body["model"] == "embedder", "the alias, never the model -- repinning is config, not code"


async def test_no_metadata_key_when_there_is_no_task(monkeypatch):
    seen = _responder(monkeypatch, _vectors)
    await embeddings.aembed(["a"], _config())
    assert "metadata" not in json.loads(seen[0].content)


async def test_a_router_error_is_raised_not_swallowed(monkeypatch):
    """The caller's right answer depends on knowing: an episode write persists
    without a vector, a search drops its leg. An empty list would look like a
    corpus with no matches."""
    _responder(monkeypatch, lambda request, n: httpx.Response(502, json={"error": {"message": "no"}}))
    with pytest.raises(embeddings.EmbeddingError) as e:
        await embeddings.aembed(["a"], _config())
    assert "502" in str(e.value)


async def test_an_unreachable_router_is_raised(monkeypatch):
    def refuse(request, n):
        raise httpx.ConnectError("connection refused")

    _responder(monkeypatch, refuse)
    with pytest.raises(embeddings.EmbeddingError):
        await embeddings.aembed(["a"], _config())


async def test_a_vector_of_the_wrong_width_is_refused(monkeypatch):
    """The model is swappable in the router's config.yaml and the column it is
    stored in is not. A silently short vector matches nothing and explains
    nothing."""
    _responder(monkeypatch, lambda request, n: _vectors(request, n, dims=8))
    with pytest.raises(embeddings.EmbeddingError) as e:
        await embeddings.aembed(["a"], _config())
    assert "EMBEDDING_DIMS" in str(e.value)


async def test_a_short_answer_is_refused(monkeypatch):
    """Fewer vectors than inputs would otherwise pair each item with someone
    else's embedding from that point on."""
    _responder(monkeypatch, lambda request, n: httpx.Response(
        200, json={"data": [{"index": 0, "embedding": [0.0] * 4}]}))
    with pytest.raises(embeddings.EmbeddingError):
        await embeddings.aembed(["a", "b"], _config())


async def test_a_response_that_is_not_an_embedding_is_refused(monkeypatch):
    _responder(monkeypatch, lambda request, n: httpx.Response(200, json={"object": "list"}))
    with pytest.raises(embeddings.EmbeddingError):
        await embeddings.aembed(["a"], _config())
