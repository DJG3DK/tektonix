"""The sandbox's caps come from the environment, with the shared host's as defaults."""
import importlib


def _reload(monkeypatch, **env):
    for k in ("SANDBOX_CPUS", "SANDBOX_MEMORY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from agent.tools import sandbox
    return importlib.reload(sandbox)


def test_defaults_are_the_shared_host_s(monkeypatch):
    sb = _reload(monkeypatch)
    assert (sb.SANDBOX_CPU_LIMIT, sb.SANDBOX_MEMORY_LIMIT) == ("2", "2g")
    assert sb.SANDBOX_TEST_ENV == {"DJANGO_TEST_PROCESSES": "2"}


def test_a_desktop_sets_its_own(monkeypatch):
    sb = _reload(monkeypatch, SANDBOX_CPUS="6", SANDBOX_MEMORY="8g")
    assert (sb.SANDBOX_CPU_LIMIT, sb.SANDBOX_MEMORY_LIMIT) == ("6", "8g")
    assert sb.SANDBOX_MEMORY_SWAP == "8g"


def test_nonsense_falls_back_to_the_defaults(monkeypatch):
    sb = _reload(monkeypatch, SANDBOX_CPUS="all", SANDBOX_MEMORY="lots")
    assert (sb.SANDBOX_CPU_LIMIT, sb.SANDBOX_MEMORY_LIMIT) == ("2", "2g")
    _reload(monkeypatch)
