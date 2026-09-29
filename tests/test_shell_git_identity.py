"""The shell helper's minimal environment keeps git's identity. It scrubs
the process environment on purpose (agent-authored commands must not see
secrets), and that scrub dropped the identity compose sets, so every commit
in the bundle failed with "Please tell me who you are" (2026-09-29)."""
import asyncio

from agent.tools import shell


def test_the_identity_passes_the_scrub_and_secrets_do_not(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Danny")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "danny@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Danny")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "danny@example.test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    env = shell._safe_base_env()
    assert env["GIT_AUTHOR_EMAIL"] == "danny@example.test" and env["GIT_COMMITTER_NAME"] == "Danny"
    assert "OPENROUTER_API_KEY" not in env


def test_a_commit_through_the_helper_carries_the_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Danny")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "danny@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Danny")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "danny@example.test")
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))   # no global git config anywhere
    (tmp_path / "nohome").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("a\n")

    async def go():
        for cmd in ("git init -q", "git add -A", "git commit -qm 'first'"):
            r = await shell.run_shell(cmd, str(repo))
            assert r["ok"], f"{cmd}: {r['output']}"
        return await shell.run_shell("git log -1 --format='%an <%ae>'", str(repo))

    out = asyncio.run(go())
    assert out["output"].strip() == "Danny <danny@example.test>"


def test_the_sandbox_and_the_host_shell_share_one_identity_tuple(monkeypatch):
    """A variable added to one used to be missing from the other (2026-09-29
    audit, A15)."""
    from agent.tools import sandbox
    assert sandbox.GIT_IDENTITY_VARS is shell.GIT_IDENTITY_VARS
    for k in shell.GIT_IDENTITY_VARS:
        monkeypatch.setenv(k, f"v-{k}")
    env = shell._safe_base_env()
    args = sandbox.git_identity_args()
    for k in shell.GIT_IDENTITY_VARS:
        assert env[k] == f"v-{k}" and f"{k}=v-{k}" in args
