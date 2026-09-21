"""The semantic leg of episode recall: the same question, asked of a vector.

Full text answers "have we hit this before" whenever the words repeat, and
on this corpus the words usually do -- an error string, a path, a command,
a CVE id. It cannot answer the one case it was never able to: a months-old
episode about a different part of the tree that describes the same SHAPE of
problem in none of the same words. "the API answers 200 when the database
is gone" and "health endpoint does not actually check Mongo" share no
content word, and an embedding of each lands them next to each other.

That is the entire remit, and it is why this is a LEG and not a tool. It
registers in agent/episode_recall.py beside the full-text leg, the two
rankings are fused there, and nothing about search_history, the seats or
the prompts knows this exists. The operator asked for vector search; what
they get is better results from the search they already have.

Scope, deliberately narrow
--------------------------
Episodes only. Embedding the task log would cost the most per token and
return the least: it is a transcript, and a transcript's discriminating
content is exactly the identifiers full text already wins on.

And within an episode, not the episode. The stored record averages 13,851
characters of prose, shell output and paths; one 1536-dimension average
over that is a blur. What is embedded is `embed_text` -- the goal, the
outcome and the reason it failed -- built here and written by
agent/episodes.py, which is the only writer.

Why the field name matters more than it looks
---------------------------------------------
langgraph's index config names the keys to embed, and its writer calls
get_text_at_path(value, field) for each one. A value with no `embed_text`
key yields no text, `vector_values` stays empty, and the store makes no
embedding request at all. So every other writer in this system -- the
planning log rewriting a 2000-entry document every 20 seconds during a
live turn, the task rows, the settings documents -- pays nothing, with not
one line changed at any of them. The default, the whole value, would have
embedded all of it.

And the two stores do not spell that key the same way: Postgres reads
`fields`, SQLite reads `text_fields`, and each quietly falls back to the
whole value when it does not find its own. index_config() sets both, and a
test asserts what each store makes of it, because getting this wrong costs
money on every write and reports nothing.

The footgun on the read side
----------------------------
A store built with no index config does not refuse a semantic search. Its
query builder falls through to the plain branch, ignores `query` entirely
and returns rows by updated_at DESC -- plausible garbage, silently. So the
leg checks `store.index_config` itself rather than trusting asearch to
fail.

And a nearest-neighbour search has no way of saying "nothing matched": it
returns `limit` rows for any query at all. Measured on this corpus, that was
not a nuisance but the leg's main effect -- it filled every page whether or
not it had found anything, and fusion counts positions, so full text's
correct answer of "nothing" was destroyed by a ranking of near-misses. See
MIN_SIMILARITY.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from langgraph.store.base import SearchOp

from agent import embeddings, episode_recall
from agent.backends import backend_for_dsn
from agent.config import Config
from agent.episode_recall import EpisodeHit
from agent.project_removal import namespaces as _project_namespaces

logger = logging.getLogger("tektonix")

LEG_NAME = "vector"

# The corpus this leg answers for, spelled the way agent/history_index.py
# spells it so a ref from either leg opens the same record through
# read_history. Not imported from there: this module is imported by
# agent/graph.py, which has no business pulling in the full-text index to
# open a store.
CORPUS = "episode"

# The one key on a stored episode that is embedded, and the fingerprint of
# the text it was taken from. The digest is what lets a re-run of the
# backfill skip an episode: langgraph has no change detection, so a re-put
# of identical text pays for the embedding again.
EMBED_FIELD = "embed_text"
DIGEST_FIELD = "embed_digest"

# The goal is free prose a person wrote and it is the discriminating half of
# the record, but a goal that runs to ten paragraphs dilutes the average it
# lands in. 1500 characters is roughly the length past which the goals in
# this corpus stop saying anything new.
_GOAL_CHARS = 1500

# What a hit shows when nothing highlighted it. The full-text leg has
# ts_headline; a cosine distance has no fragment to point at, so the head of
# the embedded text is the honest substitute -- it IS what matched.
_SNIPPET_CHARS = 240
_LABEL_CHARS = 110

# The score below which a row is not an answer, it is just the nearest thing
# in a small corpus. A vector search always returns `limit` rows: there is no
# "no match" in a nearest-neighbour query, so without a floor this leg fills
# every page whether or not it found anything, and fusion then destroys full
# text's correct answer of "nothing matched".
#
# Measured on this corpus (146 embedded episodes, openai/text-embedding-3-small
# at 1536 dims), cosine similarity separates cleanly: two deliberately
# nonsense queries topped out at 0.2573 and 0.1603, while genuine paraphrase
# matches -- the case this leg exists for -- scored 0.5627 and 0.5358. 0.35
# sits in the empty band between them. It is a property of this corpus and
# this embedder, so re-measure it if either changes.
MIN_SIMILARITY = 0.35

# pgvector 0.6.0 caps an HNSW index at 2000 dimensions and has no halfvec.
# Asserted where the config is built rather than discovered at CREATE INDEX,
# because by then the store is half migrated and the error names an operator
# class rather than the model somebody repinned.
_MAX_HNSW_DIMS = 2000


def episodes_namespace(repo: str) -> tuple[str, ...]:
    """The store namespace episodes live in, taken from the canonical list
    rather than spelled again -- agent/project_removal.py::namespaces is what
    the removal path walks, and a second copy here is one that can drift."""
    return _project_namespaces(repo)["episodes"]


# ---------------------------------------------------------------------------
# what gets embedded
# ---------------------------------------------------------------------------

def embed_text(record: dict) -> str:
    """The text an episode is found by.

    Four fields out of eight. The goal and the reason it failed carry the
    whole signal; cost_usd and iteration_count are numbers, and a number in
    an embedding is noise wearing a value's clothes.

    Labelled rather than concatenated bare, because "shipped" and
    "escalated" as free-standing words would sit near every episode that
    happens to use them in its goal.
    """
    goal = " ".join((record.get("goal") or "").split())[:_GOAL_CHARS]
    parts = [goal] if goal else []
    for label, key in (
        ("outcome", "outcome"),
        ("failed because", "escalation_reason"),
        ("review", "review_verdict"),
    ):
        value = " ".join(str(record.get(key) or "").split())
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts)


def embed_fields(record: dict) -> dict:
    """The two keys agent/episodes.py adds to a stored episode.

    Returned as a dict rather than set by this module because episodes.py is
    the single writer of that value and owns its shape; this decides only
    what goes in the two keys.
    """
    text = embed_text(record)
    if not text:
        # No goal, no outcome, no reason: there is nothing to find this by,
        # and an empty string embeds to a vector that sits equidistant from
        # every query. Better to carry no vector at all -- get_text_at_path
        # returns nothing for an absent key and the store skips it.
        return {}
    return {EMBED_FIELD: text, DIGEST_FIELD: embeddings.digest(text)}


# ---------------------------------------------------------------------------
# the index config a store is opened with
# ---------------------------------------------------------------------------

def index_config(config: Config) -> dict | None:
    """What agent/graph.py hands the store, or None to open it as before.

    None is the normal state and it is byte-for-byte today's behaviour: no
    store_vectors table, no vector migrations, no embedding call on any
    write. The feature turns on by an operator setting EMBEDDINGS_ENABLED
    and the prerequisites being there, which is what embeddings.available()
    answers.
    """
    if not embeddings.available(config):
        return None
    if config.embedding_dims > _MAX_HNSW_DIMS:
        raise ValueError(
            f"EMBEDDING_DIMS is {config.embedding_dims} and an HNSW index tops out at "
            f"{_MAX_HNSW_DIMS} -- pin a model with fewer dimensions, or pass a `dimensions` "
            "parameter in the router's config.yaml for the `embedder` deployment"
        )
    index: dict = {
        "dims": config.embedding_dims,
        "embed": _embedder(config),
        # BOTH spellings, and this is not belt and braces. Measured in the
        # installed langgraph 1.2.11: the Postgres store's
        # _ensure_index_config reads `fields`, the SQLite store's reads
        # `text_fields`, and each falls back to ["$"] -- the whole value --
        # when it does not find its own. So a config carrying only `fields`
        # works on Postgres and silently embeds EVERY store write on SQLite,
        # including agent/planning_log.py's 2000-entry document rewritten
        # every twenty seconds during a live turn. It does not fail; it just
        # costs money forever. Keep both until one of them is gone upstream.
        "fields": [EMBED_FIELD],
        "text_fields": [EMBED_FIELD],
        # Cosine because the embeddings are unit-ish and a goal written at
        # length must not rank below a terse one for being longer.
        "distance_type": "cosine",
    }
    if backend_for_dsn(config.dsn) == "postgres":
        # vector_type stays "vector": halfvec arrived in pgvector 0.7 and
        # this cluster runs 0.6.0.
        index["ann_index_config"] = {"kind": "hnsw", "vector_type": "vector"}
    return index


def _embedder(config: Config):
    """The async callable langgraph wraps. Bound to this config so the store
    does not have to re-read the environment on every write."""

    async def embed(texts: list[str]) -> list[list[float]]:
        return await embeddings.aembed(texts, config)

    return embed


# ---------------------------------------------------------------------------
# the retrieval leg
# ---------------------------------------------------------------------------

async def vector_leg(store, repo: str, query: str, *, limit: int = 20,
                     repos: list[str] | None = None,
                     corpora: tuple[str, ...] = (CORPUS,),
                     since: datetime | None = None, **kwargs) -> list[EpisodeHit]:
    """This store's vectors, as one way of answering "have we hit this before".

    `**kwargs` swallows arguments meant for other legs on purpose: legs are
    called with one set of arguments by one caller, and a leg that raised
    TypeError over a keyword it had never heard of would take the whole
    search down with it.

    Raises rather than returning [] when the search itself fails.
    recall_episodes drops a leg that raises AND records what it said, which
    is the difference between "the corpus holds nothing" and "the embedder
    was unreachable" -- two answers a model should act on differently.
    """
    if CORPUS not in (corpora or (CORPUS,)):
        return []
    if not (query or "").strip():
        return []
    if getattr(store, "index_config", None) is None:
        # See the module docstring: asearch with no index does NOT fail, it
        # returns the most recently updated rows as though they had matched.
        return []

    # One BATCH, not a loop of asearch calls. Both stores collect the query
    # texts across a batch and embed them in a single request, so a search
    # over four projects costs one embedding round trip instead of four --
    # which on a repo='*' search was most of the leg's latency, and all of
    # it was spent asking the same question four times.
    targets = list(repos or [repo])
    pages = await store.abatch([
        SearchOp(episodes_namespace(target), None, limit, 0, query, None)
        for target in targets
    ])

    scored: list[tuple[float, str, EpisodeHit]] = []
    for target, items in zip(targets, pages, strict=True):
        for item in items or ():
            score = item.score if item.score is not None else 0.0
            if score < MIN_SIMILARITY:
                # See MIN_SIMILARITY. Dropped before ranking rather than
                # after, so a query with nothing to find contributes no
                # ranking at all instead of a full page of near-misses:
                # fusion counts POSITIONS, and a rank 1 that only means
                # "least far away" outvotes an exact full-text match.
                continue
            hit = _hit(target, item, since=since)
            if hit is not None:
                scored.append((score, target, hit))

    # Both backends return cosine SIMILARITY here, higher meaning closer, so
    # one sort merges the per-project searches onto one ranking. Ties are
    # broken by ref so the order does not depend on dict iteration.
    scored.sort(key=lambda row: (-row[0], row[2].ref))
    return [
        EpisodeHit(ref=hit.ref, repo=hit.repo, rank=n, snippet=hit.snippet,
                   leg=LEG_NAME, extra=hit.extra)
        for n, (_, _, hit) in enumerate(scored[:limit], 1)
    ]


def _hit(repo: str, item, *, since: datetime | None) -> EpisodeHit | None:
    """One store item as a hit, or None when it is out of the window.

    `since` is applied here rather than in the query because neither store's
    vector search takes a date filter, and pushing it into `filter=` would
    filter on the VALUE, where the timestamp is buried inside a JSON string.
    """
    occurred = _utc(getattr(item, "created_at", None))
    if since is not None and occurred is not None and occurred < since:
        return None
    record = _record(item)
    text = (item.value or {}).get(EMBED_FIELD) or ""
    return EpisodeHit(
        ref=f"{CORPUS}:{repo}:{item.key}",
        repo=repo,
        rank=0,  # assigned by the caller once every project's hits are merged
        snippet=_trim(text, _SNIPPET_CHARS),
        leg=LEG_NAME,
        extra={
            "corpus": CORPUS,
            "item_key": item.key,
            "occurred_at": occurred,
            "label": _trim(record.get("goal") or "", _LABEL_CHARS),
            "outcome": record.get("outcome"),
            "task_id": record.get("task_id"),
            "source_live": True,
            # Named so the fused digest can say WHY a hit that shares no
            # word with the query is in the list. Without it a correct
            # semantic hit reads as a broken search.
            "stage": "semantic",
        },
    )


def _utc(when: datetime | None) -> datetime | None:
    """A store timestamp that can be compared with an aware `since`.

    Measured, not assumed: the Postgres store hands back an aware datetime
    and the SQLite store a naive one. Compared as they come, a
    since_days-limited search on SQLite raises TypeError inside the leg,
    which recall_episodes then drops -- so the whole semantic half of the
    search disappears for exactly the queries that narrow by date, and the
    only trace is one warning line. A naive store timestamp is UTC; both
    stores write CURRENT_TIMESTAMP.
    """
    if when is None or when.tzinfo is not None:
        return when
    return when.replace(tzinfo=UTC)


def _record(item) -> dict:
    """The episode's own fields, out of the JSON document it is stored as.

    Best-effort: a record that cannot be parsed still ranks and still opens
    through read_history, it just carries no label or task id. Losing a real
    hit to a malformed document would be the worse trade.
    """
    try:
        return json.loads((item.value or {}).get("content") or "{}")
    except (ValueError, TypeError, AttributeError) as e:
        logger.debug("vector leg: episode %s is not readable JSON: %s", getattr(item, "key", "?"), e)
        return {}


def _trim(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def install(store) -> bool:
    """Register the leg when the store handed over can actually answer.

    Keyed on the STORE rather than on the config, and that is the whole
    point of the function: a store opened before an operator turned the
    feature on carries no index config, and a leg registered against it
    would return the most recently updated episodes to every query while
    looking exactly like semantic search. Called once, by whoever owns the
    process, the same way agent/history_index.py::install is.
    """
    if getattr(store, "index_config", None) is None:
        episode_recall.unregister_leg(LEG_NAME)
        return False
    episode_recall.register_leg(LEG_NAME, vector_leg)
    logger.info("episode recall: the vector leg is registered")
    return True


def available() -> bool:
    """Whether the vector leg is registered in this process.

    The capability convention (agent/capabilities.py). Deliberately a
    question about this process and not about the installation: the answer
    an operator wants when search comes back without a semantic hit is "is
    it running here", and the prerequisites are what
    agent/embeddings.py::available already reports.
    """
    return LEG_NAME in episode_recall.registered_legs()
