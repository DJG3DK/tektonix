"""Unit tests for describe_image (agent/tools/agent_tools.py).

Covers the switch from a raw httpx POST to the router's REST endpoint (invisible
to LangSmith tracing no matter how often it's called) to a LangChain
ChatOpenAI invocation -- necessary so agent-vision usage actually shows up
in the Analytics "model usage by role" scan, the same way every other
agent-* pinned role does. The actual ChatOpenAI call lives in
agent/tools/vision.py's describe_image_bytes -- shared with browse_page
(agent/tools/planning_tools.py), so it's patched there, not on
agent_tools directly.
"""

import base64

from agent.tools.agent_tools import make_agent_tools

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _describe_image_tool(tmp_path):
    tools, _ = make_agent_tools(str(tmp_path))
    by_name = {t.name: t for t in tools}
    return by_name["describe_image"]


class _FakeResponse:
    def __init__(self, content):
        self.content = content


def _fake_chat_openai_class(content=None, exc=None):
    """Stand-in for ChatOpenAI -- describe_image_bytes calls .ainvoke()
    directly (no with_structured_output wrapping), so intercepting
    ChatOpenAI itself is enough here, unlike classify.py's tests."""

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            pass

        async def ainvoke(self, messages):
            if exc:
                raise exc
            return _FakeResponse(content)

    return _FakeChatOpenAI


async def test_describes_image_via_chat_openai(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.tools.vision.ChatOpenAI",
        _fake_chat_openai_class(content="A red square on a white background."),
    )
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_BYTES)
    tool = _describe_image_tool(tmp_path)
    result = await tool.ainvoke({"path": "shot.png", "question": "what color is it?"})
    assert result == "A red square on a white background."


async def test_returns_error_string_on_call_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.tools.vision.ChatOpenAI",
        _fake_chat_openai_class(exc=ConnectionError("router unreachable")),
    )
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_BYTES)
    tool = _describe_image_tool(tmp_path)
    result = await tool.ainvoke({"path": "shot.png"})
    assert result.startswith("ERROR:")


async def test_missing_image_is_rejected_before_any_model_call(tmp_path, monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("ChatOpenAI should never be constructed for a missing file")

    monkeypatch.setattr("agent.tools.vision.ChatOpenAI", _boom)
    tool = _describe_image_tool(tmp_path)
    result = await tool.ainvoke({"path": "nope.png"})
    assert result.startswith("ERROR: image not found")


async def test_non_image_file_is_rejected_before_any_model_call(tmp_path, monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("ChatOpenAI should never be constructed for a non-image file")

    monkeypatch.setattr("agent.tools.vision.ChatOpenAI", _boom)
    p = tmp_path / "notes.txt"
    p.write_text("just text", encoding="utf-8")
    tool = _describe_image_tool(tmp_path)
    result = await tool.ainvoke({"path": "notes.txt"})
    assert result.startswith("ERROR:")
    assert "not a recognised image" in result
