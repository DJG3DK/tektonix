"""Which database a DSN names, and what the answer is allowed to change.

The whole point of agent/backends.py is that there is exactly one answer to
"is this Postgres or SQLite" in the tree. These tests pin the answer for
every spelling the design accepts, pin the refusal for everything else, and
pin the two things that have to keep being true on the live server: that
pg_dsn still hands back the DSN, and that a stray local DSN cannot boot a
server against an empty file.
"""

import pathlib

import pytest

from agent import backends
from agent.config import ALLOW_SQLITE_SERVER_VAR, Config, load_config, require_server_config


# --- classification --------------------------------------------------------

@pytest.mark.parametrize("dsn", [
    "postgresql://agent:pw@127.0.0.1:5432/agent",
    "postgres://agent:pw@127.0.0.1:5432/agent",
    "POSTGRESQL://agent@host/db",
])
def test_postgres_dsns_are_classified_as_postgres(dsn):
    assert backends.backend_for_dsn(dsn) == "postgres"


@pytest.mark.parametrize("dsn", [
    "sqlite:///state.db",
    "sqlite:////var/lib/state.db",
    "sqlite+aiosqlite:///state.db",
])
def test_sqlite_dsns_are_classified_as_sqlite(dsn):
    assert backends.backend_for_dsn(dsn) == "sqlite"


@pytest.mark.parametrize("dsn", ["", "mysql://h/d", "sqlit:///x.db", "postgre://h/d",
                                 "/var/lib/state.db", "state.db"])
def test_anything_else_is_refused_by_name(dsn):
    """A typo must not silently pick a backend -- the message has to name
    both families so the reader can see which one they meant."""
    with pytest.raises(ValueError) as e:
        backends.backend_for_dsn(dsn)
    assert "postgresql://" in str(e.value)
    assert "sqlite://" in str(e.value)


@pytest.mark.parametrize("dsn", ["postgresql+psycopg://user:hunter2@h/db",
                                 "postgres+asyncpg://user:hunter2@h/db"])
def test_a_sqlalchemy_style_postgres_url_is_refused_with_the_edit_to_make(dsn):
    """AGENT_DSN is new, and a SQLAlchemy URL is the shape people already
    have in their fingers -- sqlite+aiosqlite:// is accepted three lines
    away. Refusing it is fine; refusing it without saying that the "+driver"
    is the whole problem is not, because the message would claim the string
    does not name a database when it plainly names Postgres."""
    with pytest.raises(ValueError) as e:
        backends.backend_for_dsn(dsn)
    message = str(e.value)
    driver = dsn.split("+", 1)[1].split(":", 1)[0]
    assert f"+{driver}" in message, "the message has to name the part to delete"


@pytest.mark.parametrize("dsn", ["postgre://agent:hunter2@127.0.0.1:5432/agent",
                                 "postgresql+psycopg://agent:hunter2@127.0.0.1:5432/agent"])
def test_a_refused_dsn_never_appears_in_the_message(dsn):
    """require_server_config calls this while agent/server.py is still being
    imported, so whatever it raises is an uncaught traceback in the pm2 log
    -- and observability.py's redactor only knows the schemes that are
    spelled correctly, which a typo'd one is not. The scheme is enough to
    act on and carries no password."""
    with pytest.raises(ValueError) as e:
        backends.backend_for_dsn(dsn)
    assert "hunter2" not in str(e.value)
    assert dsn not in str(e.value)


# --- the file a sqlite DSN points at ---------------------------------------

def test_three_slashes_is_relative_and_four_is_absolute():
    assert backends.sqlite_path_from_dsn("sqlite:///state/agent.db") == pathlib.Path("state/agent.db")
    assert backends.sqlite_path_from_dsn("sqlite:////var/lib/agent.db") == pathlib.Path("/var/lib/agent.db")


def test_an_in_memory_database_is_refused():
    """Each connection would get its own private empty database, which looks
    exactly like data loss from the outside."""
    with pytest.raises(ValueError):
        backends.sqlite_path_from_dsn("sqlite:///:memory:")


def test_a_postgres_dsn_is_not_a_sqlite_path():
    with pytest.raises(ValueError):
        backends.sqlite_path_from_dsn("postgresql://h/d")


async def test_opening_a_sqlite_connection_without_the_package_says_how_to_get_it(monkeypatch):
    monkeypatch.setattr(backends, "sqlite_available", lambda: False)
    with pytest.raises(RuntimeError) as e:
        await backends.open_sqlite_conn("/tmp/never-created.db")
    assert backends.SQLITE_INSTALL_HINT in str(e.value)


# --- Config.pg_dsn ---------------------------------------------------------

def _config(dsn: str) -> Config:
    return load_config(dsn=dsn)


def test_pg_dsn_still_returns_the_dsn_on_postgres():
    """The live server reads pg_dsn in five places. This is the assertion
    that turning it into a property changed nothing for them."""
    dsn = "postgresql://agent:pw@127.0.0.1:5432/agent"
    assert _config(dsn).pg_dsn == dsn


def test_pg_dsn_raises_something_an_operator_can_act_on_for_sqlite():
    with pytest.raises(RuntimeError) as e:
        assert _config("sqlite:///state.db").pg_dsn
    message = str(e.value)
    assert "Postgres" in message
    assert "sqlite:///state.db" in message, "the message has to say what it found, not just what it wanted"


def test_the_dsn_defaults_to_the_variable_the_live_box_already_sets(monkeypatch):
    monkeypatch.delenv("AGENT_DSN", raising=False)
    monkeypatch.setenv("LANGGRAPH_PG_DSN", "postgresql://only:here@127.0.0.1:5432/agent")
    assert load_config().dsn == "postgresql://only:here@127.0.0.1:5432/agent"


def test_agent_dsn_overrides_it_when_someone_sets_it(monkeypatch):
    monkeypatch.setenv("AGENT_DSN", "sqlite:///state.db")
    assert load_config().dsn == "sqlite:///state.db"


def test_embedding_settings_are_off_by_default(monkeypatch):
    for name in ("EMBEDDINGS_ENABLED", "EMBEDDING_ALIAS", "EMBEDDING_DIMS"):
        monkeypatch.delenv(name, raising=False)
    config = load_config()
    assert config.embeddings_enabled is False
    assert config.embedding_alias == "embedder"
    assert config.embedding_dims == 1536


# --- require_server_config -------------------------------------------------

def test_the_server_refuses_a_local_dsn(monkeypatch):
    """Without this the dashboard boots against an empty file and every
    project reads as having no tasks."""
    monkeypatch.delenv(ALLOW_SQLITE_SERVER_VAR, raising=False)
    with pytest.raises(RuntimeError) as e:
        require_server_config(_config("sqlite:///state.db"))
    assert ALLOW_SQLITE_SERVER_VAR in str(e.value)


def test_the_refusal_can_be_overridden_deliberately(monkeypatch):
    monkeypatch.setenv(ALLOW_SQLITE_SERVER_VAR, "1")
    require_server_config(_config("sqlite:///state.db"))


def test_every_missing_variable_is_reported_at_once(monkeypatch):
    """One restart per missing variable is how this used to go."""
    for name in ("AUTH_SECRET_KEY", "SMTP_HOST", "SMTP_USER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError) as e:
        require_server_config(_config("postgresql://h/d"))
    message = str(e.value)
    assert "AUTH_SECRET_KEY" in message and "SMTP_HOST" in message and "SMTP_USER" in message


def test_a_fully_configured_postgres_server_passes():
    """The live shape: every server variable present, a Postgres DSN, and
    nothing to say about it."""
    require_server_config(_config("postgresql://agent@127.0.0.1:5432/agent"))


def test_a_variable_set_to_empty_counts_as_configured(monkeypatch):
    """os.environ[NAME] never minded an empty value, and neither does this
    -- a relay that wants no credentials is configured, not forgotten."""
    monkeypatch.setenv("SMTP_USER", "")
    require_server_config(_config("postgresql://agent@127.0.0.1:5432/agent"))
