"""H8: behavioural tests for the controls that had none.

These guard settings where a one-word change silently removes a protection and
every other test still passes: the secret scrub on the agent's shell env, and
the container hardening flags. Both were previously unverified — flipping
inherit_env to True, or dropping --cap-drop ALL, broke nothing detectable.

Deliberately behavioural, not source-text assertions. Two existing tests
inspect getsource() for substrings, which this project's own guidance rejects:
a source-text test passes as soon as the string is present, whether or not the
code does what the string implies.
"""

import inspect

import pytest

from agent.tools import sandbox
from agent.tools.shell import _safe_base_env, run_shell


SECRETS = {
    "OPENROUTER_API_KEY": "sk-or-v1-should-never-leak",
    "MODEL_ROUTER_KEY": "sk-master-should-never-leak",
    "AUTH_SECRET_KEY": "auth-should-never-leak",
    "LANGGRAPH_PG_DSN": "postgresql://user:password@host/db",
    "REVIEW_CONTROL_SECRET": "control-should-never-leak",
    "SMTP_PASS": "smtp-should-never-leak",
    "TELEGRAM_BOT_TOKEN": "telegram-should-never-leak",
}


@pytest.fixture
def with_secrets(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)


def test_the_agent_shell_env_carries_no_secrets(with_secrets):
    """The agent runs model-chosen commands. Anything in that process's
    environment is readable by `env`, so the base env is an allow-list."""
    env = _safe_base_env()
    blob = "\n".join(f"{k}={v}" for k, v in env.items())
    for name, value in SECRETS.items():
        assert value not in blob, f"{name}'s value reached the agent's shell env"
        assert name not in env, f"{name} itself is present in the agent's shell env"


def test_the_safe_env_still_provides_what_commands_need(with_secrets):
    """A scrub that removes PATH would break every command and get reverted;
    the allow-list has to stay useful."""
    env = _safe_base_env()
    assert env.get("PATH"), "PATH must survive the scrub"
    assert env.get("CI") == "true"


async def test_run_shell_does_not_leak_secrets_to_the_command(with_secrets):
    """End to end: run a real command that dumps its environment."""
    res = await run_shell("env", "/tmp", timeout=30)
    assert res["ok"], res["output"]
    for name, value in SECRETS.items():
        assert value not in res["output"], f"{name} leaked into a shell command"


async def test_inherit_env_is_opt_in_and_off_by_default(with_secrets):
    """The parameter exists for deterministic host-side callers. The default
    must stay False — flipping it is a one-word change that would otherwise
    hand every secret to model-chosen commands."""
    sig = inspect.signature(run_shell)
    assert sig.parameters["inherit_env"].default is False


def test_container_argv_carries_the_hardening_flags(monkeypatch):
    """--cap-drop ALL and no-new-privileges are the container's teeth. Dropping
    either changes nothing observable in any other test."""
    captured = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        return _Proc()

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", fake_exec)

    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        sandbox.run_shell_sandboxed("true", "/tmp", timeout=5)
    )

    argv = list(captured["argv"])
    assert argv[0] == "docker" and argv[1] == "run"
    assert "--rm" in argv
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv and "no-new-privileges" in argv[argv.index("--security-opt") + 1]
    assert "--pids-limit" in argv, "no pids limit: a fork bomb takes the host down"
    assert "--memory" in argv and "--cpus" in argv
    # the workspace is the only thing mounted by default
    assert "-w" in argv and argv[argv.index("-w") + 1] == "/workspace"


def test_network_isolation_is_passed_through_when_requested(monkeypatch):
    """The deterministic check runner asks for --network none; that request
    has to actually reach docker."""
    captured = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        return _Proc()

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", fake_exec)

    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        sandbox.run_shell_sandboxed("true", "/tmp", timeout=5, network="none")
    )
    argv = list(captured["argv"])
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
