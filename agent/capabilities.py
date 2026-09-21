"""What this installation can actually do, and what it is missing.

Parts of this system are optional by design. The logo tools need a node
package a fresh clone does not have; the GitHub tools need a token; the
local database backend needs a requirements file the server deliberately
does not install; episode recall needs an index that has to be built. Each
one is absent rather than broken when its prerequisite is missing, and the
seat that would have used it is simply not offered it -- a tool that can
only ever fail is worse than no tool, because a model will keep trying it.

The cost of that design is a question an operator cannot answer from the
outside: search returns nothing, and there is no way to tell whether the
corpus is empty, the index was never built, or the feature was never
installed. Three subsystems each grew their own way of answering it.

So: one convention. An optional subsystem exposes `available() -> bool`,
its tool factory returns [] when that is False, and it is listed here so
`scripts/doctor.py` prints one line per capability. The probes import lazily
because doctor.py is the thing you run when the installation is broken, and
it has to survive a subsystem that cannot even be imported.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    name: str
    # What stops working when this is absent -- the operator's question is
    # never "is X installed", it is "why did the thing I just tried do
    # nothing".
    provides: str
    probe: Callable[[], bool]
    # What to do about it. Empty when absence is a normal, supported state
    # that needs no action.
    hint: str = ""

    def available(self) -> bool:
        """Never raises: an optional subsystem that cannot even be imported
        is unavailable, which is the same answer by a different route."""
        try:
            return bool(self.probe())
        except Exception:  # noqa: BLE001 -- see the docstring
            return False


def _logo_kit() -> bool:
    from agent.tools.logo_tools import installed  # noqa: PLC0415

    return installed()


def _github_tools() -> bool:
    from agent.config import load_config  # noqa: PLC0415
    from agent.tools.github_tools import available  # noqa: PLC0415

    return available(load_config())


def _sqlite_backend() -> bool:
    from agent.backends import sqlite_available  # noqa: PLC0415

    return sqlite_available()


def _episode_recall() -> bool:
    from agent import history_index  # noqa: PLC0415
    from agent.episode_recall import available  # noqa: PLC0415

    # Two ways to be true, because the answer depends on who is asking. In
    # the server a leg is registered when an index is installed, so the
    # registry is the live answer. doctor.py is its own short-lived process
    # that installs nothing, so for it the question is whether there is an
    # index for a leg to register against -- which is what the server will
    # find at its next start.
    return available() or history_index.available()


def _history_index() -> bool:
    from agent.history_index import available  # noqa: PLC0415

    return available()


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "logo kit", "designing a mark and exporting a brand kit", _logo_kit,
        "npm ci in services/logoloom",
    ),
    Capability(
        "github tools", "reading pull requests and issues from a task", _github_tools,
        # Two ways to have a token and only one of them is visible from a
        # command line: the per-project tokens are held in the server's own
        # process. So the hint names both rather than sending an operator
        # who already stored one in the dashboard to go and set a variable.
        "set GITHUB_TOKEN, or store a token in Settings -> GitHub -- a per-project "
        "token is only visible to the running server, not from here",
    ),
    Capability(
        "sqlite backend", "running against a local file instead of a database server",
        _sqlite_backend, "pip install -r requirements-cli.txt -- not needed on a server",
    ),
    Capability(
        "episode recall", "searching what past tasks ran into", _episode_recall,
        # Absent means the seats that would search history are built without
        # search_history at all, which is the intended state on an
        # installation with no index -- not an error to chase.
        "needs the history index below; a second (vector) leg is not built yet",
    ),
    Capability(
        "history index", "keeping past episodes, tasks and build transcripts searchable",
        _history_index,
        # Two states are both "not available" and only one of them is a
        # problem: the table is created at the first server start, so on a
        # box that has never run the server there is nothing to fix.
        "the table is created when the server starts; fill it with "
        ".venv/bin/python scripts/backfill_history_index.py --apply",
    ),
)
