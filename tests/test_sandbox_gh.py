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


# ---------------------------------------------------------------------------
# acting on a finding means reading the code, not the analyser
# ---------------------------------------------------------------------------
#
# Live 2026-09-22: a task handed a CodeQL alert with sixteen exact file:line
# locations spent twenty-five minutes downloading SqlInjection.qll and
# express.qll, delegated a subagent to research the query further, and never
# opened the controller. The two fixes were three lines each.
#
# The prompt was not neutral about this -- its DELEGATE RESEARCH block tells
# the coordinator to hand off "the moment that costs more than a couple of
# looks". The agent obeyed faithfully, on the wrong question. So the guidance
# has to reach the coordinator AND the investigator that executes the
# delegation, or the handoff just relocates the rabbit hole.

def test_the_finding_guidance_reaches_every_prompt_that_can_chase_one():
    import agent.deep_agent as d

    marker = "ACTING ON A REPORTED FINDING"
    assert marker in d.COORDINATOR_SYSTEM_PROMPT_TEMPLATE, "the coordinator decides what to delegate"
    # Every prompt built from the shared guidance blocks gets it, which is
    # what puts it in front of the investigator too.
    source = (paths.REPO_ROOT / "agent" / "deep_agent.py").read_text()
    assert source.count("_FILESYSTEM_GUIDANCE + _VISUAL_GUIDANCE + _FINDING_GUIDANCE") == 3


def test_it_says_go_to_the_line_before_anything_else():
    from agent.deep_agent import _FINDING_GUIDANCE as g
    assert "Open THAT file at THAT line, first, before anything else" in g


def test_it_names_the_specific_trap_rather_than_moralising():
    """'Be efficient' would not have stopped this. Naming the exact behaviour
    -- reading the analyser's own source -- is what makes it actionable."""
    from agent.deep_agent import _FINDING_GUIDANCE as g
    assert "DO NOT go and read the tool that produced the finding" in g
    for specific in ("rule definitions", "query source", "extension packs"):
        assert specific in g


def test_it_does_not_over_correct_into_never_investigate():
    """Investigation is usually right; the distinction is the code versus the
    tool. A block that read as 'stop investigating' would break far more than
    it fixed."""
    from agent.deep_agent import _FINDING_GUIDANCE as g
    assert "What IS worth investigating is the CODE" in g
    assert "`investigator` subagent is the right place for them" in g


def test_it_tells_a_delegated_subagent_to_push_back():
    """The coordinator not asking is half the fix; the investigator not
    running with it when asked is the other half."""
    from agent.deep_agent import _FINDING_GUIDANCE as g
    assert "handed that question as a delegation" in g


def test_it_gives_an_exit_rather_than_leaving_the_agent_stuck():
    """Without a bound, 'do not research the tool' turns an over-researching
    agent into a stalled one."""
    from agent.deep_agent import _FINDING_GUIDANCE as g
    assert "look the rule up ONCE" in g
    assert "honest partial fix" in g


def test_the_shared_guidance_blocks_contain_no_braces():
    """A brace in a shared block breaks one prompt or the other, and the
    failure is 35 unrelated tests away from the edit that caused it.

    COORDINATOR_SYSTEM_PROMPT_TEMPLATE goes through .format(); the
    investigator and test-writer prompts do not. The same block lands in all
    three, so a literal `{ email }` raises KeyError(' email ') in the first
    and doubling it to `{{ email }}` renders literally in the other two.
    Caught 2026-09-22 by exactly that KeyError.
    """
    import agent.deep_agent as d

    for name in ("_FINDING_GUIDANCE", "_FILESYSTEM_GUIDANCE", "_VISUAL_GUIDANCE"):
        block = getattr(d, name)
        assert "{" not in block and "}" not in block, f"{name} contains a brace"


def test_the_coordinator_prompt_still_formats():
    """The direct assertion of the bug: this raised KeyError(' email ')."""
    import agent.deep_agent as d

    out = d.COORDINATOR_SYSTEM_PROMPT_TEMPLATE.format(
        repo="demo", memory_path="/memories/AGENTS.md", org_memory_path="/org-memory/ORG.md",
        project_memory_content="", org_memory_content="", skills_summary="")
    assert "ACTING ON A REPORTED FINDING" in out
    # And nothing rendered as a doubled brace on the way through.
    assert "{{" not in out and "}}" not in out
