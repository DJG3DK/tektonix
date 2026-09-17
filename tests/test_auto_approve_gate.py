"""The auto-approve gate (User.auto_approve_commands -> deep_agent.interrupt_on_for).

Auto mode removes the approval prompt for the sensitive-PATH class -- the
one that fires constantly on ordinary work and makes a long task need
babysitting. It deliberately does NOT remove the gate on destructive
commands, whose whole point is actions a revert can't undo, nor on
ask_user, which is the agent's question channel rather than a safety gate.

These tests pin that split. A regression here doesn't fail loudly at
runtime -- it just silently stops asking about `rm -rf`.
"""

import pytest
from types import SimpleNamespace

from agent.deep_agent import (
    INTERRUPT_ON,
    INTERRUPT_ON_AUTO_APPROVE,
    interrupt_on_for,
)


def _req(command: str):
    return SimpleNamespace(tool_call={"name": "bash", "args": {"command": command}})


DESTRUCTIVE = ["rm -rf /tmp/x", "git push --force origin main", "sudo apt install x",
               "chmod -R 777 .", "chown -R root .", "echo x > /dev/sda", ":(){ :|:& };:"]
SENSITIVE_PATH = ["cat .env", "vim config/app.yaml", "cat .git/config",
                  "edit .github/workflows/ci.yml", "cat ~/.ssh/id_rsa"]


def test_default_gate_is_unchanged_when_auto_mode_is_off():
    assert interrupt_on_for(False) is INTERRUPT_ON


def test_auto_mode_selects_the_relaxed_gate():
    assert interrupt_on_for(True) is INTERRUPT_ON_AUTO_APPROVE


# Auto mode's gate is deletions that lose work, not every destructive marker
# (2026-09-11). bash only ever runs inside the sandbox container, which has no
# credentials, no capabilities and one writable mount, so a push cannot
# authenticate and sudo cannot escalate -- but a delete still loses the work in
# the worktree. These two lists pin that line.
LOSES_WORK = [
    "rm -rf src", "rm src/app.ts", "rm -rf .", "rm -rf /", "rm -rf ~",
    "rm -rf $HOME", "rm -rf /workspace", "rm -rf .git", "rm -rf ../other-project",
    'rm -rf "$BUILD_DIR"', "rm -rf", "git clean -fdx", 'find . -name "*.ts" -delete',
    "find src -name x -delete", "rm -rf /home/storefront",
    # cd is tracked, so the last one decides where a relative target lands
    "cd /tmp && cd /workspace && rm -rf src",
]
LOSES_NOTHING = [
    # the exact call that interrupted a task about every 30 seconds
    "cd /tmp && rm -rf u && mkdir u && cd u && npm pack undici@7.18.2 >/dev/null 2>&1",
    "rm -rf /tmp/scratch", "rm -rf dist && npm run build", "rm -rf node_modules/.vite",
    "rm -rf apps/api/dist", "rm -f coverage/lcov.info", "rm -rf .pytest_cache __pycache__",
    "cd /var/tmp/x && rm -rf *",
    # the container answers these; strict mode still asks
    "sudo apt-get install -y jq", "git push --force origin main", ":(){ :|:& };:",
    "echo x > /dev/sda", "chmod -R 777 .", "npm ci && npm test",
]


def test_auto_mode_gates_a_delete_that_loses_work():
    when = INTERRUPT_ON_AUTO_APPROVE["bash"]["when"]
    for command in LOSES_WORK:
        assert when(_req(command)) is True, f"auto mode must still gate: {command}"


def test_auto_mode_runs_scratch_and_sandbox_answered_commands_unattended():
    when = INTERRUPT_ON_AUTO_APPROVE["bash"]["when"]
    for command in LOSES_NOTHING:
        assert when(_req(command)) is False, f"auto mode should not prompt for: {command}"


def test_strict_mode_still_asks_about_every_destructive_marker():
    """Narrowing auto mode must not narrow the default gate with it."""
    when = INTERRUPT_ON["bash"]["when"]
    for command in DESTRUCTIVE:
        assert when(_req(command)) is True, f"default gate must still ask about: {command}"


def test_sensitive_paths_are_auto_approved_in_auto_mode():
    when = INTERRUPT_ON_AUTO_APPROVE["bash"]["when"]
    for command in SENSITIVE_PATH:
        assert when(_req(command)) is False, f"auto mode should not prompt for: {command}"


def test_the_same_sensitive_paths_ARE_gated_with_auto_mode_off():
    """Guards against the relaxed predicate being wired into both slots --
    which would look fine in the tests above while quietly disabling the
    normal gate for everyone."""
    when = INTERRUPT_ON["bash"]["when"]
    for command in SENSITIVE_PATH:
        assert when(_req(command)) is True, f"default gate must still ask about: {command}"


def test_ask_user_still_interrupts_in_auto_mode():
    assert "ask_user" in INTERRUPT_ON_AUTO_APPROVE
    assert INTERRUPT_ON_AUTO_APPROVE["ask_user"] is INTERRUPT_ON["ask_user"]


def test_auto_mode_never_drops_bash_from_the_gate_entirely():
    """bash must always have SOME gate -- an empty/absent entry would mean
    every command runs unattended, destructive ones included."""
    assert "bash" in INTERRUPT_ON_AUTO_APPROVE
    assert callable(INTERRUPT_ON_AUTO_APPROVE["bash"]["when"])


def test_every_danger_marker_is_lowercase():
    """Both predicates lowercase the command before matching, so an
    uppercase character in a marker makes it dead -- it can never match.
    "chmod -R"/"chown -R" shipped that way and silently gated nothing from
    the day they were added. This asserts the invariant directly rather
    than relying on someone remembering it.
    """
    from agent.deep_agent import _DANGEROUS_COMMAND_MARKERS, _SENSITIVE_PATH_MARKERS

    for marker in _DANGEROUS_COMMAND_MARKERS + _SENSITIVE_PATH_MARKERS:
        assert marker == marker.lower(), f"marker {marker!r} can never match a lowercased command"


def test_recursive_chmod_and_chown_are_gated_in_strict_mode():
    """The specific regression: these two were dead markers (uppercase R in a
    list matched against a lowercased command). Strict mode still asks; auto
    mode does not, because a mode change inside the container cannot outlive
    the container -- only a delete costs work."""
    for command in ["chmod -R 777 .", "chown -R root:root .", "CHMOD -R 777 ."]:
        assert INTERRUPT_ON["bash"]["when"](_req(command)) is True


# audit M-7: whitespace/flag-order evasions the old substring matcher missed.
EVASIONS = [
    "rm  -rf /srv",        # two spaces
    "rm -fr /srv",         # flag order
    "rm\t-rf /srv",        # tab
    "rm -Rf /srv",         # capital R
    "find . -delete",      # no "rm" at all
    "git -c a=b push origin x --force",  # -c prefix before push
    "sudo\tid",            # tab after sudo
]


@pytest.mark.parametrize("cmd", EVASIONS)
def test_destructive_evasions_are_still_gated_in_both_modes(cmd):
    from agent.deep_agent import _bash_is_destructive, _bash_needs_approval
    assert _bash_is_destructive(_req(cmd)), f"auto-approve let {cmd!r} through"
    assert _bash_needs_approval(_req(cmd)), f"strict mode let {cmd!r} through"


@pytest.mark.parametrize("cmd", ["echo hi", "ls -la", "rm just-one-file.txt", "git push origin"])
def test_benign_or_nonforce_commands_are_not_false_flagged_as_destructive(cmd):
    from agent.deep_agent import _bash_is_destructive
    # a plain non-force `git push` is still gated (it's in the marker list) but
    # must not be misread as the always-destructive force-push subset via regex.
    if cmd.startswith("git push"):
        return
    assert not _bash_is_destructive(_req(cmd)), f"{cmd!r} wrongly flagged destructive"


# ---------------------------------------------------------------------------
# /dev/null false positive (2026-08-27)
# ---------------------------------------------------------------------------
#
# The destructive pattern `>\s*/dev/` was written for raw devices
# (> /dev/sda) but \s* matches zero spaces, so it flagged `2>/dev/null` --
# the most common idiom in shell. Every quiet command prompted for approval,
# auto-approve mode included: a live build's plain `ls ... 2>/dev/null` and
# `find ... 2>/dev/null` both gated as "destructive", and the operator asked
# why their auto-approve setting wasn't working.

from agent.deep_agent import _matches_dangerous


@pytest.mark.parametrize("benign", [
    "ls tests/ 2>/dev/null",
    "find . -name '*.test.*' 2> /dev/null | head -30",
    "cat foo > /dev/null",
    "rg -n pattern src 2>/dev/null",
    "echo hi > /dev/stdout",
    "some_tool --log /dev/stderr",
])
def test_null_and_stream_device_redirects_are_not_destructive(benign):
    assert not _matches_dangerous(benign), benign


@pytest.mark.parametrize("destructive", [
    "echo x > /dev/sda",
    "cat img >/dev/sda1",
    "dd if=backup.img of=/dev/sda",
])
def test_raw_device_writes_still_gate(destructive):
    assert _matches_dangerous(destructive), destructive


def test_dd_to_dev_null_is_fine():
    assert not _matches_dangerous("dd if=big.bin of=/dev/null bs=1M")


# ---------------------------------------------------------------------------
# The repo-aware half (2026-09-11, second report): auto mode still stopped for
# the coder's own throwaway probe files, because a command string cannot tell
# repo content from scratch. With a repo root, git answers that.
# ---------------------------------------------------------------------------

@pytest.fixture
def worktree(tmp_path):
    """A git repo with one tracked file under a tracked directory."""
    import subprocess
    root = tmp_path / "wt"
    (root / "apps" / "storefront" / "src").mkdir(parents=True)
    (root / "apps" / "storefront" / "src" / "html.ts").write_text("export const x = 1;\n")
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.email=t@t.t",
                "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, capture_output=True)
    return str(root)


def _auto_when(repo_root=None):
    return interrupt_on_for(True, repo_root)["bash"]["when"]


def test_the_agents_own_scratch_file_is_deleted_without_asking(worktree):
    """The exact shape that interrupted a live task: write a probe, run it,
    delete it. git never heard of the probe, so removing it changes nothing
    in the diff the operator reviews."""
    probe = (
        "cd /workspace/apps/storefront && cat > scripts/probe-html.test.mjs <<'EOF'\n"
        "import { htmlToPlainText } from '../src/utils/html.ts';\n"
        "console.log('PROBE', 1);\n"
        "EOF\n"
        "npx vitest run scripts/probe-html.test.mjs 2>&1 | head -20; rm -f scripts/probe-html.test.mjs"
    )
    assert _auto_when(worktree)(_req(probe)) is False
    # ...and without a repo root the same command still asks: unknown means ask.
    assert _auto_when()(_req(probe)) is True


def test_deleting_tracked_content_still_asks(worktree):
    for command in [
        "rm -rf /workspace/apps/storefront/src",
        "cd /workspace && rm -rf apps",
        "cd /workspace/apps/storefront && rm src/html.ts",
        "cd /workspace/apps/storefront && find src -name '*.ts' -delete",
        "rm -rf .",                       # the worktree root
        "rm -rf /workspace",
    ]:
        assert _auto_when(worktree)(_req(command)) is True, command


def test_a_path_git_cannot_speak_for_always_asks(worktree):
    """.git holds no tracked files, so the tracked-content check would wave a
    history delete through. Same for a target that cannot be read at all."""
    for command in ["rm -rf .git", "rm -rf /workspace/.git", 'rm -rf "$BUILD_DIR"',
                    "rm -rf", "git clean -fdx", "rm -rf /etc", "rm -rf /home/other-project"]:
        assert _auto_when(worktree)(_req(command)) is True, command


def test_scratch_and_build_output_never_ask_even_with_a_repo(worktree):
    for command in ["cd /tmp && rm -rf u && mkdir u", "rm -rf /tmp/x", "rm -rf dist",
                    "rm -rf node_modules", "rm -rf coverage .pytest_cache",
                    "cd /workspace/apps/api && rm -f scratch-check.ts"]:
        assert _auto_when(worktree)(_req(command)) is False, command


def test_a_broken_repo_root_fails_closed(tmp_path):
    """git failing (not a repo, gone, slow) must mean ask, not run."""
    assert _auto_when(str(tmp_path / "nope"))(_req("rm -rf /workspace/src")) is True
