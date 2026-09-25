"""CODER_REASONING=off turns the coder seat's chain of thought off for this
process and nothing else (agent/deep_agent.py, 2026-09-25)."""
import agent.deep_agent as da


def test_off_reaches_the_request_body_and_the_default_does_not(monkeypatch):
    from agent.config import load_config
    cfg = load_config()
    monkeypatch.delenv("CODER_REASONING", raising=False)
    assert da.coder_reasoning() is None
    plain = da.llm_for_role(cfg, "agent-coder", reasoning=da.coder_reasoning(), task_id="t")
    assert "reasoning" not in (plain.extra_body or {})
    monkeypatch.setenv("CODER_REASONING", "off")
    assert da.coder_reasoning() is False
    off = da.llm_for_role(cfg, "agent-coder", reasoning=da.coder_reasoning(), task_id="t")
    assert off.extra_body["reasoning"] == {"enabled": False}
    assert off.extra_body.get("metadata"), "the ledger's task metadata must survive the merge"
