"""Unit tests for the LangSmith trace redaction anonymizer -- the backstop
for the fact that this system's agent-facing `bash` tool can read arbitrary
files inside its sandbox, and a tool result containing a real secret would
otherwise be traced to LangSmith's servers verbatim. Written during a
follow-up audit specifically of the LangSmith addition (distinct from the
earlier full LangGraph/deepagents docs audit).

Uses real-shaped secrets matching this exact deployment's own formats
(confirmed live against agent/observability.py's actual redaction output
during development, not just asserted here) -- these are NOT the real
values, just the same shape/format.
"""

import pytest

from agent.observability import _build_anonymizer


def _redact(text: str) -> str:
    result = _build_anonymizer()({"content": text})
    return result["content"]


def test_redacts_key_value_pairs_with_suspicious_key_names():
    assert "[REDACTED]" in _redact("api_key: abc123supersecret")
    assert "abc123supersecret" not in _redact("api_key: abc123supersecret")
    assert "[REDACTED]" in _redact('password="hunter2verylong"')
    assert "[REDACTED]" in _redact("secret=verylongsecretvalue123")


def test_redacts_postgres_dsn_password_but_keeps_rest_of_dsn():
    dsn = "postgresql://three_d_agent:ffffffffffffffffffffffffffffffffffffffffffffffff@localhost:5432/three_d_agent"
    result = _redact(dsn)
    assert "ffffffffffffffffffffffffffffffffffffffffffffffff" not in result
    assert "[REDACTED]" in result
    # Host/port/dbname aren't secrets -- keep them, only the password is redacted.
    assert "localhost:5432/three_d_agent" in result


def test_redacts_sk_style_key_even_without_a_suggestive_key_name():
    result = _redact("raw key with no prefix: sk-router-fakekeyfakekeyfakekeyfakekeyfakekeyfake01")
    assert "fakekeyfakekeyfakekeyfakekeyfakekeyfake01" not in result
    assert "[REDACTED]" in result


def test_redacts_langsmith_key_shape_including_the_underscore_separator():
    # Regression test: the char class originally excluded "_", and the real
    # key format (lsv2_pt_<32-hex>_<12-hex>) has one partway through the
    # suffix -- the whole match silently failed, not just a partial redaction.
    result = _redact("langsmith key lsv2_pt_notarealkeyvalue_pastthe_underscore")
    assert "notarealkeyvalue_pastthe_underscore" not in result
    assert "[REDACTED]" in result


def test_redacts_aws_access_key():
    result = _redact("AWS key AKIAIOSFODNN7EXAMPLE in the config")
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert "[REDACTED]" in result


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    result = _redact(f"token: {jwt}")
    assert jwt not in result


def test_redacts_pem_private_key_block_entirely():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA1234567890abcdefg\n-----END RSA PRIVATE KEY-----"
    result = _redact(pem)
    assert "MIIEpAIBAAKCAQEA1234567890abcdefg" not in result


def test_redacts_bearer_token():
    result = _redact("Authorization: Bearer abc123verylongtoken4567890")
    assert "abc123verylongtoken4567890" not in result
    assert "[REDACTED]" in result


def test_leaves_ordinary_text_untouched():
    text = "nothing sensitive here, just normal text about pair identity BTCUSDT_long"
    assert _redact(text) == text


def test_recurses_into_nested_message_structures():
    # Real trace shape: a dict of lists of dicts (messages), not a flat string.
    data = {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "tool", "content": [{"type": "text", "text": "api_key: sk-router-realkeyvaluehere1234567890"}]},
        ]
    }
    result = _build_anonymizer()(data)
    assert "sk-router-realkeyvaluehere1234567890" not in str(result)
    assert result["messages"][0]["content"] == "hello"


# ---------------------------------------------------------------------------
# Security audit 2026-08-24: the key-name rule was effectively dead
# ---------------------------------------------------------------------------
# The pattern anchored on \b, but `_` is a word character, so the anchor could
# never match inside a namespaced identifier. Bare API_KEY= redacted; every
# realistic name -- OPENROUTER_API_KEY, DOBA_PRIVATE_KEY, SMTP_PASS, MY_SECRET
# -- passed through in the clear, which is nearly all of them in practice.

SECRET = "abc123supersecretvalue"


@pytest.mark.parametrize("key", [
    "API_KEY", "OPENROUTER_API_KEY", "LANGSMITH_API_KEY",
    "MY_SECRET", "AUTH_SECRET_KEY",
    "DOBA_PRIVATE_KEY", "private_key",
    "SMTP_PASS", "DB_PASS", "DB_PASSWORD", "password", "passwd",
    "agent_session", "SESSION_TOKEN", "cookie",
    "ACCESS_KEY", "AWS_ACCESS_KEY", "auth_token", "CREDENTIAL",
])
def test_namespaced_secret_names_are_redacted(key):
    assert SECRET not in _redact(f"{key}={SECRET}")


@pytest.mark.parametrize("text", [
    "the access_token flow is documented in README",
    "tokens: 1500",
    "session started at noon",
    "password reset instructions are in the docs",
])
def test_ordinary_prose_is_not_mangled(text):
    """The widened prefix must not turn every mention of these words into
    [REDACTED] -- the rule still requires an actual `key = value` shape."""
    assert _redact(text) == text


# ---------------------------------------------------------------------------
# Streaming: the agent pays for what it reads, and it reads snapshots
# ---------------------------------------------------------------------------

def test_tool_bearing_calls_are_not_streamed():
    """Measured 2026-09-12: one 734-token tool call cost ~25 CPU-seconds, all
    of it langchain-core reassembling the stream. Every chunk re-runs
    AIMessageChunk's init_tool_calls validator, which re-parses the whole
    accumulated argument JSON -- 25 chunks cost 0.02s, 200 cost 2.73s, while
    800 text-only chunks cost 0.009s.

    Nothing reads those chunks: work.py and planning_chat.py both consume
    `run.values`, a state snapshot per superstep, so the dashboard shows
    messages as they complete and never token by token.
    """
    from agent.config import load_config
    from agent.deep_agent import llm_for_role

    model = llm_for_role(load_config(), "agent-coder")
    assert model.disable_streaming == "tool_calling"
    # and a call with no tools bound still streams, which is cheap
    assert model.stream_usage is True, "a streamed call still has to report usage"


def test_nothing_consumes_token_level_events():
    """The claim the setting above rests on. If a future change starts reading
    chunk events, the trade changes and this test should fail first."""
    import pathlib

    for rel in ("agent/nodes/work.py", "agent/planning_chat.py"):
        src = pathlib.Path(rel).read_text()
        assert "run.values" in src, f"{rel} no longer consumes state snapshots"
        for token_level in ("on_chat_model_stream", "astream_log", ".astream_tokens"):
            assert token_level not in src, f"{rel} now consumes {token_level}: re-examine disable_streaming"
