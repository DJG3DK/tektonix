"""Turning text into a vector, through the router like every other model call.

Semantic recall over episodes needs one thing this system did not have: an
embedding. It does NOT need a new provider, a new credential or a local
model. The router already holds an OpenRouter key and already proxies chat
calls there, and OpenRouter serves embeddings on the same host with the same
response envelope -- measured 2026-09-21: openai/text-embedding-3-small
returns 1536 dimensions and a `usage.cost` of 1.4e-07 for 7 tokens.

That last detail is why this goes through the router rather than calling
OpenRouter directly from here, which would have been three fewer lines.
Spend in this system is whatever services/model-router/logs/routing.jsonl
says was billed, never a rate table, so a model call made outside the router
is money the operator's totals cannot see. An embedding on every terminal
task is small money forever, which is exactly the kind that goes missing.

Three things stop this costing more than it is worth:

  * it is off unless an operator turns it on (Config.embeddings_enabled),
    and available() says so rather than letting a call fail at write time;
  * identical text is embedded once per call, keyed by digest, and a caller
    that stored a digest alongside its text can skip the call entirely --
    see unchanged();
  * a batch is one request, because the API takes a list of inputs. The
    backfill of the whole corpus is a few requests, not a few hundred.

What this module deliberately does NOT do is decide anything about storage.
The vector column, the index and the recall leg belong to the code that
reads them back; this is the embedder and nothing else.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence

import httpx

from agent.backends import backend_for_dsn, sqlite_available
from agent.config import Config, load_config

logger = logging.getLogger("tektonix")

# How many inputs go in one request. OpenAI's own limit is far higher; this
# is about the size of the HTTP body and about how much work one failure
# throws away, on a corpus whose whole backfill is ~150 digests.
MAX_BATCH = 64

# The client's patience, which has to exceed the ROUTER's. The router gives a
# deployment three tries at its configured timeout (30s for `embedder`)
# before falling back, so a shorter wait here would abandon a call the router
# is still making -- and still paying for.
_TIMEOUT_S = 120.0

# The availability probe's own timeout. It runs in scripts/doctor.py, on a
# box where the thing being diagnosed may be the router being down, so it
# fails fast rather than hanging the report.
_PROBE_TIMEOUT_S = 5.0

_probe_cache: dict[tuple, bool] = {}


class EmbeddingError(RuntimeError):
    """An embedding could not be produced.

    Named and raised rather than returned as an empty list because the
    caller's correct response depends on which it is: at write time an
    episode must still be persisted without a vector, and a silent [] there
    would store a record that looks embedded and is not.
    """


def digest(text: str) -> str:
    """A stable fingerprint of the text that was embedded.

    Stored next to the vector by whoever writes one, so the next write can
    ask whether anything actually changed. langgraph's store has no change
    detection of its own: re-putting an item re-embeds it, however identical
    the text. On an append-only corpus that never fires, but a backfill that
    is re-run -- and it is designed to be re-runnable -- would otherwise pay
    for the entire corpus a second time.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unchanged(text: str, stored_digest: str | None) -> bool:
    """Whether `text` is what `stored_digest` was taken from.

    False when nothing was stored, which is the honest answer for a record
    written before digests existed: it has to be embedded once before it can
    be skipped.
    """
    return bool(stored_digest) and stored_digest == digest(text)


def available(config: Config | None = None) -> bool:
    """Whether this installation can embed anything at all.

    The capability convention (agent/capabilities.py): an optional subsystem
    answers for itself, the code that would use it asks first, and
    scripts/doctor.py prints the answer -- so an operator whose semantic
    search returns nothing is told which of the three prerequisites is
    missing instead of reading three modules.

    Three ways to be false, and only the first is a decision rather than a
    gap: the feature is off, the router has no `embedder` deployment, or the
    database cannot hold a vector. The last one is checked here rather than
    at first write because on Postgres the app role cannot create the
    extension itself (it is not superuser and `vector` is not a trusted
    extension), so an installation that never ran that one command deserves
    a doctor line, not a crash in the episode writer.

    Cached per process, like agent/tools/logo_tools.py's installed(): it is
    asked once per seat build and the answer cannot change without a
    restart of the router or a change to this process's config.
    """
    config = config or load_config()
    key = (config.embeddings_enabled, config.router_base_url, config.embedding_alias, config.dsn)
    if key not in _probe_cache:
        _probe_cache[key] = _probe(config)
    return _probe_cache[key]


def reset_probe_cache() -> None:
    """Forget what available() decided. For tests, and for a process that has
    just been told the router config changed."""
    _probe_cache.clear()


def _probe(config: Config) -> bool:
    if not config.embeddings_enabled:
        return False
    if not _router_has_alias(config):
        return False
    if not _router_serves_embeddings(config):
        return False
    return _store_can_hold_vectors(config)


def _router_has_alias(config: Config) -> bool:
    """Whether the router serves the embedding alias.

    /v1/models is the router's own list of what config.yaml defines, so this
    is the same question as "is there an `embedder` entry", asked of the
    process that would answer the call rather than of a file on disk that
    may not be the one it loaded.
    """
    try:
        r = httpx.get(
            f"{config.router_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {config.router_api_key}"},
            timeout=_PROBE_TIMEOUT_S,
        )
        r.raise_for_status()
        ids = {m.get("id") for m in (r.json().get("data") or [])}
    except Exception as e:  # noqa: BLE001 -- an unreachable router is an unavailable capability
        logger.debug("embedding probe: router unreachable: %s", e)
        return False
    return config.embedding_alias in ids


def _router_serves_embeddings(config: Config) -> bool:
    """Whether the router's /v1/embeddings route exists at all.

    A separate question from the alias, and the reason is a real state this
    box was in: config.yaml already named `embedder`, so /v1/models listed
    it, while the running router process predated the endpoint and answered
    404. On that installation available() said yes, and every episode write
    made a doomed call that was caught, warned about and dropped -- exactly
    the "search returns nothing and nothing says why" shape the capability
    convention exists to close.

    Asked with no model, which the route rejects with a 400 before it spends
    anything. So this costs one local request and no money, and only a 404
    means absent: a 401 or a 400 both prove the route is there.
    """
    url = f"{config.router_base_url.rstrip('/')}/embeddings"
    try:
        r = httpx.post(url, headers={"Authorization": f"Bearer {config.router_api_key}"},
                       json={}, timeout=_PROBE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 -- an unreachable router is an unavailable capability
        logger.debug("embedding probe: router unreachable at %s: %s", url, e)
        return False
    if r.status_code == 404:
        logger.debug("embedding probe: %s answers 404 -- the router needs restarting", url)
        return False
    return True


def _store_can_hold_vectors(config: Config) -> bool:
    """Whether the database can store what this produces.

    Two backends, two different answers to the same question, and only the
    Postgres one needs a human. pgvector is an extension the app role cannot
    create for itself -- it is not superuser and `vector` is not a trusted
    extension -- so an installation that never ran that one command gets a
    doctor line here rather than a crash in the episode writer. sqlite-vec
    needs nobody: it is a pip wheel with the extension compiled in, it
    arrives with langgraph-checkpoint-sqlite, and the only thing that can be
    missing is the CLI requirements file.
    """
    if backend_for_dsn(config.dsn) == "sqlite":
        # Not just "is the package importable": some distro Python builds
        # ship sqlite3 with enable_load_extension compiled out, and on one
        # of those the wheel imports cleanly and the store's setup() fails
        # at the load. Asked of this interpreter, which is the one that
        # would do the loading.
        return sqlite_available() and _sqlite_can_load_extensions()
    import psycopg  # noqa: PLC0415 -- doctor.py runs this on a box that may have no server

    try:
        with psycopg.connect(config.pg_dsn, connect_timeout=5) as conn:
            row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    except Exception as e:  # noqa: BLE001 -- an unreachable database is an unavailable capability
        logger.debug("embedding probe: database unreachable: %s", e)
        return False
    return bool(row)


def _sqlite_can_load_extensions() -> bool:
    import sqlite3  # noqa: PLC0415 -- stdlib, but only this branch cares

    return hasattr(sqlite3.Connection, "enable_load_extension")


async def aembed(
    texts: Sequence[str],
    config: Config | None = None,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
) -> list[list[float]]:
    """Embed each text, in order, one request per batch.

    This is the callable langgraph's index config wraps, so its contract is
    fixed: a vector per input, in the input's order. Duplicates within one
    call are embedded once and handed back to every position that asked for
    them -- a backfill page of episodes from one project repeats goal text
    far more often than a corpus of prose would.

    `task_id` and `session_id` travel as routing metadata, which the router
    records and does not forward upstream. Without them the ledger can price
    an embedding but never total it into the task that caused it.

    Raises EmbeddingError rather than returning short. The caller at write
    time retries its put without an index and keeps the episode; a caller at
    read time drops its leg and lets full text answer. Both of those need to
    know, and neither can tell from a list.
    """
    config = config or load_config()
    if not texts:
        return []

    # One slot per distinct text, in first-seen order: the request is built
    # from the distinct ones and the answer is fanned back out.
    order: list[str] = []
    by_digest: dict[str, list[float]] = {}
    for text in texts:
        d = digest(text)
        if d not in by_digest:
            by_digest[d] = []
            order.append(text)

    for start in range(0, len(order), MAX_BATCH):
        batch = order[start:start + MAX_BATCH]
        for text, vector in zip(batch, await _embed_batch(batch, config, task_id, session_id), strict=True):
            by_digest[digest(text)] = vector

    return [by_digest[digest(text)] for text in texts]


async def _embed_batch(
    batch: Sequence[str], config: Config, task_id: str | None, session_id: str | None,
) -> list[list[float]]:
    body: dict = {"model": config.embedding_alias, "input": list(batch)}
    metadata = {k: v for k, v in (("agent_task_id", task_id), ("agent_session_id", session_id)) if v}
    if metadata:
        body["metadata"] = metadata

    url = f"{config.router_base_url.rstrip('/')}/embeddings"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {config.router_api_key}"},
                json=body,
            )
    except Exception as e:  # noqa: BLE001 -- surfaced as EmbeddingError, which every caller handles
        raise EmbeddingError(f"the router did not answer {url}: {type(e).__name__}: {e}") from e

    if r.status_code >= 400:
        raise EmbeddingError(f"{config.embedding_alias} failed: HTTP {r.status_code} {r.text[:300]}")
    try:
        data = r.json()["data"]
    except (ValueError, KeyError, TypeError) as e:
        raise EmbeddingError(f"{config.embedding_alias} answered without embeddings: {r.text[:300]}") from e

    # Ordered by `index`, never by arrival. The OpenAI shape carries one and
    # the whole contract of this function is that position N of the answer
    # belongs to position N of the request.
    vectors = [row["embedding"] for row in sorted(data, key=lambda row: row.get("index", 0))]
    if len(vectors) != len(batch):
        raise EmbeddingError(
            f"asked {config.embedding_alias} for {len(batch)} embeddings and got {len(vectors)}"
        )
    for vector in vectors:
        if len(vector) != config.embedding_dims:
            # The model is swappable in the router's config.yaml, and the
            # column this is stored in is not: a 3072-wide vector written
            # into a 1536-wide index fails somewhere much later, or worse,
            # a silently truncated one matches nothing and explains nothing.
            raise EmbeddingError(
                f"{config.embedding_alias} returned {len(vector)}-dimension vectors and this "
                f"installation stores {config.embedding_dims} -- repin the model in the router's "
                "config.yaml, or set EMBEDDING_DIMS and rebuild the index to match"
            )
    return vectors
