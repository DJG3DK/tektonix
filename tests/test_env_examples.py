"""The two .env examples name every knob the code reads.

docker/.env.example had drifted from the compose file (SANDBOX_CPUS,
SANDBOX_MEMORY, TEKTONIX_FEATURES were read and undocumented), and
.env.example from agent/config.py. A setting the example does not show
is a setting an operator finds by reading source, or never.
"""
from __future__ import annotations

import re

import pytest

from agent import paths

REPO = paths.REPO_ROOT


def _documented(example: str) -> set[str]:
    """Keys the example sets or shows commented out (`#   KEY=`)."""
    return set(re.findall(r"^#?\s*([A-Z_][A-Z0-9_]*)=", example, re.M))


def test_the_bundle_example_names_every_host_variable_compose_reads():
    compose = (REPO / "docker-compose.yml").read_text()
    read = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose))
    # Set by the desktop app in its own .env, never by an operator.
    read -= {"TEKTONIX_DESKTOP"}
    documented = _documented((REPO / "docker/.env.example").read_text())
    missing = sorted(read - documented)
    assert not missing, f"compose reads these from .env and docker/.env.example does not show them: {missing}"
    # The header of the compose file names these two for pre-rename installs.
    assert {"COMPOSE_PROJECT_NAME", "POSTGRES_DB"} <= documented


def test_the_host_example_names_every_key_the_agent_s_config_reads():
    keys: set[str] = set()
    for module in ("agent/config.py", "agent/features.py"):
        src = (REPO / module).read_text()
        keys |= set(re.findall(r'os\.(?:environ\.get|getenv|environ)\(?\[?"([A-Z_][A-Z0-9_]*)"', src))
    # Set by compose for the bundle and by nothing on a host install.
    keys -= {"AGENT_PROJECTS_JSON"}
    documented = _documented((REPO / ".env.example").read_text())
    missing = sorted(keys - documented)
    assert not missing, f"agent/config.py reads these and .env.example does not show them: {missing}"


@pytest.mark.parametrize("key", ["SANDBOX_CPUS", "SANDBOX_MEMORY", "TEKTONIX_FEATURES", "PUID", "PGID"])
def test_the_bundle_knobs_the_audit_found_missing_are_shown(key):
    assert key in _documented((REPO / "docker/.env.example").read_text()), key
