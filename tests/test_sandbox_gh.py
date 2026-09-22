"""`gh` in the sandbox: present, and deliberately not logged in.

Observed 2026-09-22: a task fixing a code-scanning finding opened with
`gh auth status` to find out whether it had the GitHub CLI. That was the
right instinct -- probe, then route around -- and it cost two tool calls to
learn "no such command".

The fix has two halves and only one of them is the image. Installing `gh`
without saying what it can reach would trade `command not found` for
`not logged in`, which looks more like something worth fighting.
"""
from pathlib import Path

import pytest

from agent import paths

DOCKERFILE = paths.REPO_ROOT / "docker" / "agent-sandbox" / "Dockerfile"


def test_the_sandbox_image_installs_the_github_cli():
    assert "gh" in DOCKERFILE.read_text().split()


def test_the_image_does_not_bake_in_a_github_token():
    """The security line this whole design holds: the bash tool runs
    LLM-chosen commands, so a token inside the container is a token a
    prompt-injected instruction can push with. The server-side github tools
    hold it outside the sandbox instead."""
    text = DOCKERFILE.read_text()
    for leak in ("GITHUB_TOKEN", "GH_TOKEN", "gh auth login", "--with-token"):
        assert leak not in text, f"{leak} must never appear in the sandbox image"


def test_no_github_token_is_passed_into_a_sandboxed_command():
    """Asserted against the runner, not just the image: an env var added at
    the call site would defeat the Dockerfile being clean."""
    source = (paths.REPO_ROOT / "agent" / "tools" / "sandbox.py").read_text()
    for leak in ("GITHUB_TOKEN", "GH_TOKEN", "github_token"):
        assert leak not in source, f"{leak} reaches the sandbox from sandbox.py"


def test_the_bash_tool_passes_no_extra_env_of_its_own():
    """agent/tools/agent_tools.py is what the model's `bash` calls land in.
    It must not widen the environment the checks runner carefully narrowed."""
    import re
    source = (paths.REPO_ROOT / "agent" / "tools" / "agent_tools.py").read_text()
    call = re.search(r"run_shell_sandboxed\((.*?)\)", source, re.S)
    assert call, "the bash tool no longer calls run_shell_sandboxed"
    assert "extra_env" not in call.group(1), "the bash tool now injects an environment"


def test_the_prompt_says_gh_is_unauthenticated_and_names_the_alternative():
    """Without this the agent trades one wasted probe for another: `gh` now
    exists, so it tries, gets 'not logged in', and goes looking for a token
    that is deliberately absent."""
    from agent.deep_agent import _FILESYSTEM_GUIDANCE as guidance

    assert "`gh` IS installed" in guidance
    assert "NOT logged in" in guidance
    # It has to name what to use instead, or "don't use gh" is half an answer.
    assert "github_pull_request" in guidance and "github_inbox_items" in guidance
    # And say not to go hunting, which is the specific loop being prevented.
    assert "gh auth login" in guidance


@pytest.mark.skipif(not Path("/var/run/docker.sock").exists(), reason="needs docker")
def test_gh_is_actually_in_the_built_image():
    """The Dockerfile saying so is not the same as the image having it."""
    import subprocess

    from agent.tools.sandbox import SANDBOX_IMAGE

    proc = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", SANDBOX_IMAGE, "-c", "gh --version"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "gh version" in proc.stdout


@pytest.mark.skipif(not Path("/var/run/docker.sock").exists(), reason="needs docker")
def test_the_built_image_has_no_github_credentials():
    import subprocess

    from agent.tools.sandbox import SANDBOX_IMAGE

    proc = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", SANDBOX_IMAGE, "-c",
         "gh auth status 2>&1 || true"],
        capture_output=True, text=True, timeout=120)
    assert "not logged into any GitHub hosts" in proc.stdout


def test_the_prompt_says_scratch_files_do_not_survive_between_bash_calls():
    """The same class of gap as `gh`, and it cost more. Every bash call is its
    own container, so /tmp is wiped between them -- observed 2026-09-22, when
    a task curled a CodeQL file to /tmp, got `sed: can't read express.qll` on
    the next call, and re-downloaded it, paying a 150-second model call each
    round. The prompt warned about this for SERVERS and never for files.
    """
    from agent.deep_agent import _FILESYSTEM_GUIDANCE as guidance

    assert "EVERY `bash` CALL IS ITS OWN CONTAINER" in guidance
    # It must say where to put something you need next call, or it is only
    # half the answer.
    assert "/workspace" in guidance and "persists" in guidance
    # ...and how to keep it within one call.
    assert "&&" in guidance
