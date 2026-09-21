"""Which database this installation is pointed at, decided in one place.

The server runs on Postgres and always will: a multi-worker HTTP process
needs pooled connections and a claim on a project that every process on
every host can see. The CLI that ships later has neither, and a single user
on one laptop should not have to install a database server to run a build.
So both backends have to exist, and something has to say which one a DSN
means.

That "something" is this module, and it is deliberately the ONLY one. The
first draft of this overhaul had three private copies of the same two-line
check -- one in the store opener, one in the history index, one in the
embedding probe -- and three copies of a predicate drift. The failure that
produces is not a crash: it is a SQLite deployment where two subsystems
think they are on Postgres and one knows better, so half the features come
up and nobody can say why. tests/test_repo_hygiene.py bans the literal
scheme check anywhere else under agent/ for exactly that reason.

Selection is by DSN scheme alone rather than a separate backend field,
because two sources of truth have a state where they disagree and nothing
resolves it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Literal

Backend = Literal["postgres", "sqlite"]

# Spelled out rather than matched on a prefix: a typo like "sqlit://" or
# "postgre://" must fail loudly and name what it should have been, not fall
# through to whichever branch a loose prefix test happened to reach first.
_POSTGRES_SCHEMES = ("postgresql", "postgres")
_SQLITE_SCHEMES = ("sqlite", "sqlite+aiosqlite")

# What a sqlite DSN needs before it can open anything. Kept here as one
# string because it is what both sqlite_available()'s callers and
# open_sqlite_conn()'s error have to tell an operator, and two spellings of
# an install command is one spelling too many.
SQLITE_INSTALL_HINT = "pip install -r requirements-cli.txt"


def _scheme(dsn: str) -> str:
    head, sep, _ = (dsn or "").partition("://")
    return head.lower() if sep else ""


# The tail every refusal below ends with. One sentence, in one place,
# because the reader's next move is the same whatever they typed wrong.
_EXPECTED = (
    "expected a postgresql:// or postgres:// DSN for a server installation, "
    "or a sqlite:// or sqlite+aiosqlite:// DSN for a local one"
)


def backend_for_dsn(dsn: str) -> Backend:
    """Which backend a DSN names.

    Raises ValueError naming both supported families rather than guessing.

    The refusal quotes the SCHEME and never the DSN. This runs on the
    server's boot path -- require_server_config calls it while agent/server.py
    is still importing -- so anything it raises lands in the pm2 log as an
    uncaught traceback, and a DSN carries a password. The redactor in
    agent/observability.py would not save it either: it matches known
    schemes, and a mistyped scheme is exactly the case here.
    """
    scheme = _scheme(dsn)
    if scheme in _POSTGRES_SCHEMES:
        return "postgres"
    if scheme in _SQLITE_SCHEMES:
        return "sqlite"
    if not scheme:
        raise ValueError(f"this is not a database DSN -- it has no scheme at all: {_EXPECTED}")
    # SQLAlchemy spells the driver into the scheme and people have that in
    # their fingers, so "postgresql+psycopg://" is the likeliest thing to
    # land in a brand-new AGENT_DSN. It is not accepted: the string is
    # handed to libpq further down, which cannot parse the "+psycopg" and
    # would fail a layer deeper with a message about the wrong thing. So it
    # is refused here, by name, with the edit to make. (sqlite+aiosqlite is
    # in _SQLITE_SCHEMES because there the path is parsed here, not handed
    # to a driver that minds.)
    family, _, driver = scheme.partition("+")
    if driver and family in _POSTGRES_SCHEMES:
        raise ValueError(
            f"{scheme}:// is a SQLAlchemy URL and this reads a libpq DSN -- drop the "
            f"'+{driver}' and use {family}://. To be explicit: {_EXPECTED}"
        )
    raise ValueError(f"{scheme}:// is not a database this build knows: {_EXPECTED}")


def sqlite_path_from_dsn(dsn: str) -> Path:
    """The database file a sqlite DSN points at.

    The slash count is SQLAlchemy's convention and people already have it in
    their fingers: three slashes is relative to the working directory
    (sqlite:///state.db), four is absolute (sqlite:////var/lib/state.db).
    An in-memory database is refused -- every caller here wants a file that
    survives the process, and ":memory:" would give each connection its own
    private empty database instead, which looks like data loss.
    """
    if backend_for_dsn(dsn) != "sqlite":
        raise ValueError(f"{dsn!r} is not a sqlite DSN")
    _, _, rest = dsn.partition("://")
    # One leading slash belongs to the scheme's own separator; whatever is
    # left is the path as written.
    rest = rest[1:] if rest.startswith("/") else rest
    if not rest or rest == ":memory:":
        raise ValueError(
            f"{dsn!r} names no database file -- use sqlite:///relative/state.db or "
            "sqlite:////absolute/state.db"
        )
    return Path(rest)


def sqlite_available() -> bool:
    """Whether the SQLite backend's optional dependencies are installed.

    The same shape as agent/tools/logo_tools.py's installed(): a predicate a
    doctor command can call before any work is attempted, so an operator is
    told what is missing instead of watching an import fail three layers
    down.

    It checks langgraph.store.sqlite rather than only aiosqlite because that
    is the import the code will actually take, and that module imports
    sqlite_vec at its own top level -- a half install (the checkpoint
    package present, sqlite_vec absent) fails at import time, not at use,
    and find_spec("aiosqlite") alone would cheerfully report it as fine.
    """
    try:
        return bool(
            importlib.util.find_spec("aiosqlite")
            and importlib.util.find_spec("langgraph.store.sqlite")
        )
    except (ImportError, ValueError):
        # find_spec raises rather than returning None when a parent package
        # is itself missing or is not a package.
        return False


async def open_sqlite_conn(path: Path | str, *, autocommit: bool = True):
    """A connection to `path` with the pragmas the library does not set.

    Neither AsyncSqliteStore.from_conn_string nor AsyncSqliteSaver.from_conn_string
    sets a single pragma, and in particular neither sets busy_timeout -- so
    two coroutines touching one file get an immediate "database is locked"
    instead of the short wait that would have resolved it. That is why
    callers build the connection here and hand it to the class, rather than
    using from_conn_string at all.

    foreign_keys is not decoration either: the store's vector table declares
    ON DELETE CASCADE back to the store table, and SQLite silently ignores
    that clause unless the pragma is on, which would leave a deleted item's
    embeddings behind to match on text that no longer exists.
    """
    if not sqlite_available():
        raise RuntimeError(
            "the SQLite backend needs langgraph-checkpoint-sqlite and aiosqlite, which are not "
            f"installed here -- {SQLITE_INSTALL_HINT}"
        )
    import aiosqlite  # noqa: PLC0415 -- optional dependency; see sqlite_available()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None is what the store's own code assumes; the
    # checkpointer wants a transactional connection instead.
    conn = await aiosqlite.connect(str(path), isolation_level=None if autocommit else "")
    for pragma in (
        "journal_mode=WAL",       # a reader and a writer at once, and it is persistent per file
        "busy_timeout=10000",     # wait for a lock rather than failing the call outright
        "synchronous=NORMAL",     # WAL's safe pairing: durable to a crash, not to power loss
        "foreign_keys=ON",        # see the docstring -- off by default, and the cascade needs it
    ):
        await conn.execute(f"PRAGMA {pragma}")
    return conn
