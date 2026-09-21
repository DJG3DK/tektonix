"""Finding the episode that already answers this.

"Have we hit this before?" is one question, so it gets one answer, however
many ways there turn out to be of looking it up. A full-text index over the
episode text is one way. An embedding of the same text is another, and it
finds the case full text cannot: a months-old episode about a different part
of the tree that shares no words with today's task but describes the same
shape of problem.

Those are LEGS, not features. Each one registers here, each one returns its
own ranking, and the rankings are fused. Two retrieval tools over one corpus
would be a routing decision a model gets wrong half the time, and would cost
its description in prompt tokens in every seat that has it.

Fusion is reciprocal rank -- position, never score. A cosine distance and a
text rank are not on the same scale and nothing sensible calibrates them
against each other; their ORDERINGS are directly comparable, and that is all
RRF uses. It also gives this module the property the whole build order
depends on: a leg that is not registered contributes nothing, so with one
leg the fused ranking IS that leg's ranking, unchanged. Adding the second
leg is additive, and removing it again is a revert rather than a migration.

Today there are no legs, and recall returns nothing. That is the skeleton:
the shape is agreed before anything is built against it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tektonix")


@dataclass(frozen=True)
class EpisodeHit:
    """One episode a leg thinks is relevant.

    `ref` is the store key the episode was written under (see
    agent/episodes.py), and it is what a caller quotes back to read the whole
    thing. `rank` is this leg's own ordering, 1-based -- fusion uses it and
    nothing else, which is why no leg has to explain its score to any other.
    """

    ref: str
    repo: str
    rank: int
    snippet: str = ""
    leg: str = ""
    extra: dict = field(default_factory=dict)


# A leg is asked a question and returns its own ranked answer. It is given
# the store because the legs that exist in a local installation have nowhere
# else to read from.
Leg = Callable[..., Awaitable[list[EpisodeHit]]]

_LEGS: dict[str, Leg] = {}

# RRF's smoothing constant, 60 in the paper that introduced it and in most
# implementations since. It is what stops the top hit of one leg from
# outweighing every other leg combined.
_RRF_K = 60


def register_leg(name: str, leg: Leg) -> None:
    """Add a retrieval leg. Re-registering a name replaces it, so a module
    that is imported twice does not vote twice."""
    _LEGS[name] = leg


def unregister_leg(name: str) -> None:
    _LEGS.pop(name, None)


def registered_legs() -> tuple[str, ...]:
    return tuple(sorted(_LEGS))


def available() -> bool:
    """Whether recall can answer anything at all.

    The convention agent/tools/logo_tools.py established: an optional
    subsystem says whether it works, the factory that builds its tools
    returns nothing when it does not, and scripts/doctor.py prints the
    answer -- so an operator whose search comes back empty is told why
    rather than left to read three modules and guess.
    """
    return bool(_LEGS)


def fuse(rankings: list[list[EpisodeHit]], limit: int | None = None) -> list[EpisodeHit]:
    """Reciprocal-rank fusion of several legs' rankings into one.

    An episode found by two legs beats one found by either alone; an episode
    found by one leg keeps that leg's ordering relative to its own results.
    With a single ranking in, the order out is that ranking's order.
    """
    scores: dict[str, float] = {}
    best: dict[str, EpisodeHit] = {}
    for ranking in rankings:
        for hit in ranking:
            scores[hit.ref] = scores.get(hit.ref, 0.0) + 1.0 / (_RRF_K + hit.rank)
            # Keep the hit that ranked highest anywhere: its snippet is the
            # one most likely to show the reader why this came back.
            if hit.ref not in best or hit.rank < best[hit.ref].rank:
                best[hit.ref] = hit
    order = sorted(scores, key=lambda ref: (-scores[ref], best[ref].rank, ref))
    fused = [
        EpisodeHit(
            ref=best[ref].ref,
            repo=best[ref].repo,
            rank=n,
            snippet=best[ref].snippet,
            leg=best[ref].leg,
            extra=best[ref].extra,
        )
        for n, ref in enumerate(order, 1)
    ]
    return fused[:limit] if limit else fused


async def recall_episodes(store, repo: str, query: str, *, limit: int = 20, **kwargs) -> list[EpisodeHit]:
    """Ask every registered leg, fuse what comes back.

    A leg that raises is dropped rather than allowed to fail the search: a
    broken index must degrade retrieval, never break the task that was
    merely curious.
    """
    rankings: list[list[EpisodeHit]] = []
    for name, leg in sorted(_LEGS.items()):
        try:
            rankings.append(await leg(store, repo, query, limit=limit, **kwargs))
        except Exception as e:  # noqa: BLE001 -- see the docstring
            logger.warning("episode recall: leg %s failed: %s", name, e)
    return fuse(rankings, limit=limit)


# --- telemetry -------------------------------------------------------------
#
# Whether the second leg is worth building is a question about this corpus,
# not about retrieval in general -- it is small and it grows slowly, and full
# text may simply be enough. The only way to answer it is to record what was
# asked and whether the answer got used, starting now, so that by the time
# the decision is due there is a number rather than an argument.
#
# The measure is deliberately narrow: of the searches that ran, how many
# returned nothing the task went on to read. Two lines per search, appended,
# trimmed by size, and never allowed to raise -- the same contract as
# agent/tool_events.py, for the same reason.

LOG_PATH = Path(
    os.environ.get("AGENT_RETRIEVAL_LOG")
    or (Path(__file__).resolve().parents[1] / "logs" / "retrieval_events.jsonl")
)
MAX_BYTES = 5_000_000
_KEEP_FRACTION = 0.5

# How many of the returned refs are written down. The question being asked
# is "was there a hit in the top few", so the tail costs bytes and answers
# nothing.
TOP_N = 5


def record_query(query: str, repo: str, refs: list[str], *, task_id: str | None = None,
                 legs: tuple[str, ...] | None = None, path: Path | None = None) -> None:
    """One search happened, and these are the refs it put in front of the
    model. Never raises."""
    _append({
        "ts": time.time(),
        "event": "query",
        "repo": repo,
        "task_id": task_id,
        # Truncated, not hashed: reading back why a search missed needs the
        # words, and a query is the model's own text, not anyone's secret.
        "query": (query or "")[:300],
        "legs": list(legs) if legs is not None else list(registered_legs()),
        "refs": list(refs)[:TOP_N],
        "n_hits": len(refs),
    }, path)


def record_use(ref: str, repo: str, *, task_id: str | None = None, path: Path | None = None) -> None:
    """An episode a search offered was actually read. Never raises."""
    _append({
        "ts": time.time(),
        "event": "use",
        "repo": repo,
        "task_id": task_id,
        "ref": ref,
    }, path)


# The same two questions, asked of project memory instead of episodes: which
# sections did the prompt offer, and which of them did the task actually open.
# Memory sections and episode recall are the same wager made twice -- that an
# index is a cheaper way to carry knowledge than the knowledge itself -- and
# the wager is only settleable with a record of what was offered against what
# was used. An index entry that is offered a hundred times and never read is
# either a section nobody needs or, far more likely, an entry that does not
# say what it is for; both are fixes, and neither is visible without this.


def record_sections_offered(repo: str, offered: list[str], *, always: list[str] | None = None,
                            task_id: str | None = None, path: Path | None = None) -> None:
    """A prompt was built carrying this project's memory index. Never raises."""
    _append({
        "ts": time.time(),
        "event": "memory_offered",
        "repo": repo,
        "task_id": task_id,
        "sections": list(offered),
        # Recorded separately because a pinned section cannot be "missed":
        # it was already in the prompt, so it never needed a read and its
        # absence from the read events means nothing.
        "always": list(always or []),
    }, path)


def record_section_read(slug: str, repo: str, *, task_id: str | None = None,
                        path: Path | None = None) -> None:
    """A section the index advertised was actually read. Never raises."""
    _append({
        "ts": time.time(),
        "event": "memory_read",
        "repo": repo,
        "task_id": task_id,
        "section": slug,
    }, path)


def _append(entry: dict, path: Path | None) -> None:
    target = path or LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _trim(target)
    except Exception as e:  # noqa: BLE001 -- telemetry must not break what it measures
        logger.debug("retrieval event not recorded: %s", e)


def _trim(path: Path) -> None:
    """Halve the file when it passes the cap, keeping the newest lines."""
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        with open(path, "rb") as f:
            data = f.read()
        keep = data[int(len(data) * (1 - _KEEP_FRACTION)):]
        keep = keep[keep.find(b"\n") + 1:] if b"\n" in keep else b""
        with open(path, "wb") as f:
            f.write(keep)
    except Exception as e:  # noqa: BLE001
        logger.debug("retrieval event log not trimmed: %s", e)
