"""An empty bash result must say what happened, not just show an exit code.

The exit-1 half of this (rg/grep finding nothing) already had NO_MATCHES_RESULT.
This is the exit-0 half: a command that ran, succeeded, and printed nothing
used to arrive as "exit_code=0" and a blank line, which is what a tool that
failed to capture output also looks like.
"""

from agent.tools.agent_tools import _describe_output


def test_success_with_no_output_says_so():
    body = _describe_output({"exit_code": 0, "output": ""})
    assert "exited 0" in body and "NO MATCHES" in body
    assert "Re-running it will return this same result" in body


def test_whitespace_only_counts_as_empty():
    assert "exited 0" in _describe_output({"exit_code": 0, "output": "  \n\t\n"})


def test_failure_with_no_output_does_not_claim_success():
    body = _describe_output({"exit_code": 2, "output": ""})
    assert "exited 0" not in body
    assert "non-zero exit code" in body


def test_real_output_is_passed_through_untouched():
    out = "src/App.tsx:12:  const x = 1\n"
    assert _describe_output({"exit_code": 0, "output": out}) == out
