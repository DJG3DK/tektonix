"""The sandbox's caps come from the environment, with the shared host's as defaults."""
import importlib

import pytest


@pytest.fixture
def reload(monkeypatch):
    """Reload agent.tools.sandbox with the given caps -- and again, with none,
    on teardown. A test that reloaded with SANDBOX_CPUS=6 and did not reload
    back left every later test reading a six-CPU, 8 GB sandbox (2026-09-29
    audit, A13)."""
    from agent.tools import sandbox

    def _reload(**env):
        for k in ("SANDBOX_CPUS", "SANDBOX_MEMORY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(sandbox)

    yield _reload
    _reload()
    assert sandbox.SANDBOX_CPU_LIMIT == "2"


def test_defaults_are_the_shared_host_s(reload):
    sb = reload()
    assert (sb.SANDBOX_CPU_LIMIT, sb.SANDBOX_MEMORY_LIMIT) == ("2", "2g")
    assert sb.SANDBOX_TEST_ENV == {"DJANGO_TEST_PROCESSES": "2"}


def test_a_desktop_sets_its_own(reload):
    sb = reload(SANDBOX_CPUS="6", SANDBOX_MEMORY="8g")
    assert (sb.SANDBOX_CPU_LIMIT, sb.SANDBOX_MEMORY_LIMIT) == ("6", "8g")
    assert sb.SANDBOX_MEMORY_SWAP == "8g"


def test_nonsense_falls_back_to_the_defaults(reload):
    sb = reload(SANDBOX_CPUS="all", SANDBOX_MEMORY="lots")
    assert (sb.SANDBOX_CPU_LIMIT, sb.SANDBOX_MEMORY_LIMIT) == ("2", "2g")


def test_a_fractional_cpu_cap_reaches_django_as_a_whole_number(reload):
    """docker takes `--cpus 1.5`; Django's DJANGO_TEST_PROCESSES goes through
    int(), and "1.5" crashed every Django run in the sandbox."""
    sb = reload(SANDBOX_CPUS="1.5")
    assert sb.SANDBOX_CPU_LIMIT == "1.5"
    assert sb.SANDBOX_TEST_ENV == {"DJANGO_TEST_PROCESSES": "1"}
    assert reload(SANDBOX_CPUS="0.5").SANDBOX_CPU_LIMIT == "2"          # under a core: not a cap, the default
    assert reload(SANDBOX_CPUS="3.9").SANDBOX_TEST_ENV == {"DJANGO_TEST_PROCESSES": "3"}


def test_a_test_s_reload_does_not_outlive_it(reload):
    """The fixture's teardown is what keeps one test's caps out of the next."""
    from agent.tools import sandbox
    reload(SANDBOX_CPUS="6", SANDBOX_MEMORY="8g")
    assert sandbox.SANDBOX_CPU_LIMIT == "6"
