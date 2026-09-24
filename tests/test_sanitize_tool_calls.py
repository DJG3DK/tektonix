"""SanitizeToolCallsMiddleware: a malformed tool call in history never
reaches a provider (2026-09-09: Alibaba rejected 37 planner turns with
"function.arguments must be in JSON format" after Kimi truncated one
write_todos call)."""

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai.chat_models.base import _convert_message_to_dict

import agent.deep_agent as da
import agent.planning_chat as pc
from agent.middleware.sanitize_tool_calls import SanitizeToolCallsMiddleware, sanitize_messages


def _bad_ai(content="", extra_valid=False):
    """The real shape: an invalid_tool_call carrying the raw truncated string."""
    tool_calls = [{"name": "read", "args": {"path": "a.js"}, "id": "ok:1", "type": "tool_call"}] if extra_valid else []
    return AIMessage(
        content=content,
        tool_calls=tool_calls,
        invalid_tool_calls=[{"name": "write_todos", "args": '{"todos": [{"content": "Complete visu', "id": "bad:1", "error": "truncated", "type": "invalid_tool_call"}],
        id="ai-1",
    )


PATCHED_RESULT = ToolMessage(content="Tool call write_todos with id bad:1 could not be executed - arguments were malformed or truncated.", name="write_todos", tool_call_id="bad:1")


def test_invalid_call_and_its_placeholder_result_are_removed_valid_pair_kept():
    msgs = [HumanMessage("go"), _bad_ai("working", extra_valid=True), PATCHED_RESULT,
            ToolMessage(content="file body", tool_call_id="ok:1"), AIMessage("done")]
    out, removed = sanitize_messages(msgs)
    assert removed == 1
    assert [type(m).__name__ for m in out] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    ai = out[1]
    assert ai.invalid_tool_calls == [] and [tc["id"] for tc in ai.tool_calls] == ["ok:1"]
    assert "1 malformed tool call(s) removed" in ai.content and "write_todos" in ai.content
    assert ai.id == "ai-1", "identity preserved so nothing downstream loses track of the message"
    assert out[2].tool_call_id == "ok:1"


def test_a_message_with_only_a_malformed_call_becomes_a_plain_note():
    out, removed = sanitize_messages([_bad_ai(""), PATCHED_RESULT, HumanMessage("next")])
    assert removed == 1
    assert len(out) == 2 and isinstance(out[0], AIMessage) and out[0].tool_calls == []
    assert out[0].content.startswith("[1 malformed tool call(s) removed")
    assert isinstance(out[1], HumanMessage)


def test_clean_history_is_returned_untouched():
    msgs = [SystemMessage("s"), HumanMessage("h"), AIMessage("a", tool_calls=[{"name": "read", "args": {"path": "x"}, "id": "t1", "type": "tool_call"}]), ToolMessage(content="ok", tool_call_id="t1")]
    out, removed = sanitize_messages(msgs)
    assert removed == 0 and out is msgs


def test_a_tool_call_whose_args_are_not_a_dict_counts_as_malformed():
    # The constructor validates args as a dict; a message rebuilt from an
    # older checkpoint or a foreign serializer can still carry a string.
    ai = AIMessage.model_construct(content="x", tool_calls=[{"name": "bash", "args": "not a dict", "id": "s:1", "type": "tool_call"}],
                                   invalid_tool_calls=[], additional_kwargs={}, response_metadata={}, id="m", name=None, usage_metadata=None)
    out, removed = sanitize_messages([ai, ToolMessage(content="?", tool_call_id="s:1")])
    assert removed == 1 and out[0].tool_calls == [] and len(out) == 1


def test_raw_additional_kwargs_tool_call_with_non_json_arguments_is_dropped():
    ai = AIMessage(content="", additional_kwargs={"tool_calls": [{"id": "raw:1", "type": "function", "function": {"name": "edit", "arguments": "{\"file\": "}}]})
    out, removed = sanitize_messages([ai, ToolMessage(content="?", tool_call_id="raw:1")])
    assert removed == 1
    assert "tool_calls" not in out[0].additional_kwargs and len(out) == 1


def test_block_content_gets_a_text_block_note():
    ai = AIMessage(content=[{"type": "text", "text": "thinking"}], invalid_tool_calls=[{"name": "edit", "args": "{", "id": "b:1", "error": "x", "type": "invalid_tool_call"}])
    out, _ = sanitize_messages([ai])
    assert isinstance(out[0].content, list) and out[0].content[-1]["type"] == "text" and "malformed" in out[0].content[-1]["text"]


def test_what_reaches_the_provider_is_valid_json_everywhere():
    """The condition Alibaba rejected: serialise the history the way
    langchain-openai does and check every function.arguments parses."""
    dirty = [_bad_ai("working", extra_valid=True), PATCHED_RESULT, ToolMessage(content="body", tool_call_id="ok:1")]
    before = _convert_message_to_dict(dirty[0])["tool_calls"]
    assert any(not _parses(tc["function"]["arguments"]) for tc in before), "the fixture must reproduce the bad payload"
    cleaned, _ = sanitize_messages(dirty)
    for m in cleaned:
        d = _convert_message_to_dict(m)
        for tc in d.get("tool_calls") or []:
            assert _parses(tc["function"]["arguments"]), tc


def _parses(s) -> bool:
    try:
        json.loads(s)
        return True
    except (TypeError, ValueError):
        return False


class _Req:
    def __init__(self, messages):
        self.messages = messages
        self.overridden = None

    def override(self, **kw):
        r = _Req(kw.get("messages", self.messages))
        r.overridden = kw
        return r


async def test_async_path_rewrites_only_when_needed():
    mw = SanitizeToolCallsMiddleware()
    seen = []

    async def handler(req):
        seen.append(req)
        return "ok"

    clean = _Req([HumanMessage("h")])
    await mw.awrap_model_call(clean, handler)
    assert seen[-1] is clean, "a clean request is passed through untouched"
    dirty = _Req([_bad_ai(), PATCHED_RESULT])
    await mw.awrap_model_call(dirty, handler)
    assert seen[-1] is not dirty and len(seen[-1].messages) == 1 and seen[-1].messages[0].invalid_tool_calls == []


def test_sync_path_matches():
    mw = SanitizeToolCallsMiddleware()
    got = []
    mw.wrap_model_call(_Req([_bad_ai(), PATCHED_RESULT]), lambda req: got.append(req) or "ok")
    assert len(got[0].messages) == 1


def test_sanitizer_sits_first_on_every_build_seat_and_on_planning():
    import inspect
    assert inspect.getsource(da).count("SanitizeToolCallsMiddleware()") == 5  # + the verifier
    assert inspect.getsource(pc).count("SanitizeToolCallsMiddleware()") == 1


def test_idempotent():
    once, n1 = sanitize_messages([_bad_ai("w", extra_valid=True), PATCHED_RESULT, ToolMessage(content="b", tool_call_id="ok:1")])
    twice, n2 = sanitize_messages(once)
    assert n1 == 1 and n2 == 0 and twice is once
