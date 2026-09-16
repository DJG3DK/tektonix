"""rg/grep exit 1 is "no matches", not a failure -- the bash tool says so in
its result so the model reads it right and the dashboard does not paint a
clean verification sweep red (observed 2026-09-08: four red "errors" that
were four confirmations the removed code was gone)."""

import pytest

import agent.tools.agent_tools as at
from agent.tools.agent_tools import NO_MATCHES_RESULT, _is_search_command, make_agent_tools


@pytest.mark.parametrize("cmd", [
    'cd /workspace && rg -n "PROFIT_PROTECT" src',
    "grep -rn foo src/",
    'cd /workspace && rg -rn "PP_TIMEFRAME" .',
    "cat x | grep bar",
])
def test_recognises_search_commands(cmd):
    assert _is_search_command(cmd)


@pytest.mark.parametrize("cmd", [
    "cd /workspace && npm test",
    "rg -n foo src && node scripts/run.js",  # rg is not the stage that sets the exit code
    "ls src",
])
def test_ignores_non_search_commands(cmd):
    assert not _is_search_command(cmd)


async def _bash(monkeypatch, tmp_path, exit_code, output):
    async def fake_run(command, repo_root, timeout=120):
        return {"exit_code": exit_code, "output": output}
    monkeypatch.setattr(at, "run_shell_sandboxed", fake_run)
    tools, _ = make_agent_tools(str(tmp_path))
    return {t.name: t for t in tools}["bash"]


async def test_search_with_no_output_and_exit_1_is_marked_no_matches(monkeypatch, tmp_path):
    bash = await _bash(monkeypatch, tmp_path, 1, "")
    out = await bash.ainvoke({"command": 'cd /workspace && rg -n "PROFIT_PROTECT" src'})
    assert out == NO_MATCHES_RESULT
    assert out.startswith("exit_code=1 (no matches"), "the exit code stays truthful; only the reading changes"


async def test_search_that_matched_is_untouched(monkeypatch, tmp_path):
    bash = await _bash(monkeypatch, tmp_path, 0, "src/a.js:3: PROFIT_PROTECT")
    out = await bash.ainvoke({"command": 'rg -n "PROFIT_PROTECT" src'})
    assert out == "exit_code=0\nsrc/a.js:3: PROFIT_PROTECT"


async def test_real_failures_keep_their_output(monkeypatch, tmp_path):
    bash = await _bash(monkeypatch, tmp_path, 1, "")
    out = await bash.ainvoke({"command": "npm test"})
    # The point of this case: a real failure must NOT be dressed up as
    # "no matches" -- npm test is not a search, so NO_MATCHES_RESULT must not
    # apply. It still carries the honest exit code, now with a line saying the
    # code is the only signal the command produced (see _describe_output).
    assert out.startswith("exit_code=1\n")
    assert "no matches" not in out
    assert "non-zero exit code" in out
    bash = await _bash(monkeypatch, tmp_path, 2, "rg: unrecognized flag --bogus")
    out = await bash.ainvoke({"command": "rg --bogus x"})
    assert out.startswith("exit_code=2\nrg: unrecognized")
