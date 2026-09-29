"""The compose bundle, rendered the way `docker compose up` would read it.

`docker compose config` is the offline half of the bundle job: it
interpolates the file and resolves names without touching the daemon.
What it renders is what the agent, which holds container and network
names as plain environment strings, has to agree with."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from agent import paths

REPO = paths.REPO_ROOT

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="needs the docker compose plugin",
)


def render(**env: str) -> dict:
    """The bundle as compose resolves it, under the given .env values."""
    base = {"PROJECTS_DIR": "/tmp", "OPENROUTER_API_KEY": "placeholder"}
    base.update(env)
    out = subprocess.run(
        ["docker", "compose", "--project-directory", str(REPO), "config", "--format", "json"],
        capture_output=True, text=True, check=True, cwd=REPO,
        env={**os.environ, **base, "COMPOSE_ENV_FILES": ""},  # never this box's .env
    ).stdout
    return json.loads(out)


@pytest.mark.parametrize("project_name", [None, "three-d-agent"])
def test_the_checks_network_the_agent_attaches_to_exists_under_any_project_name(project_name):
    """The agent attaches each check container by the fixed name in
    REVIEW_CHECKS_NETWORK. compose's default name carries the project name,
    so an install kept on the pre-rename project name had a
    three-d-agent_checks network and no tektonix_checks: every database
    check failed to attach."""
    env = {"COMPOSE_PROJECT_NAME": project_name} if project_name else {}
    rendered = render(**env)
    wanted = rendered["services"]["agent"]["environment"]["REVIEW_CHECKS_NETWORK"]
    names = {net.get("name") for net in rendered["networks"].values()}
    assert wanted in names, f"the agent attaches to {wanted!r}; compose creates {sorted(names)}"
    assert rendered["networks"]["checks"]["internal"] is True
