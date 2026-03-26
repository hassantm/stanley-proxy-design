"""
Unit tests for extract.py — pure functions, zero infrastructure.
"""
import pytest
from extract import (
    SSEState,
    accumulate_sse_chunks,
    extract_agent_id,
    extract_assistant_text_from_response,
    extract_model,
    extract_request_turns,
    extract_text_delta,
    infer_session_id,
    normalize_content,
    sse_feed,
)


# ---------------------------------------------------------------------------
# normalize_content
# ---------------------------------------------------------------------------

class TestNormalizeContent:
    def test_string_passthrough(self):
        assert normalize_content("hello world") == "hello world"

    def test_empty_string(self):
        assert normalize_content("") == ""

    def test_list_text_blocks(self):
        val = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
        assert normalize_content(val) == "hello\nworld"

    def test_list_ignores_non_text(self):
        val = [
            {"type": "image", "source": "..."},
            {"type": "text", "text": "kept"},
        ]
        assert normalize_content(val) == "kept"

    def test_list_no_text_blocks(self):
        assert normalize_content([{"type": "image"}]) == ""

    def test_other_type_returns_empty(self):
        assert normalize_content(42) == ""
        assert normalize_content(None) == ""


# ---------------------------------------------------------------------------
# extract_request_turns
# ---------------------------------------------------------------------------

class TestExtractRequestTurns:
    def test_basic(self):
        req = {
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ]
        }
        assert extract_request_turns(req) == [("user", "Hi"), ("assistant", "Hello")]

    def test_content_as_list(self):
        req = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello from list"}],
                }
            ]
        }
        assert extract_request_turns(req) == [("user", "Hello from list")]

    def test_skips_empty_content(self):
        req = {
            "messages": [
                {"role": "user", "content": ""},
                {"role": "user", "content": "real"},
            ]
        }
        assert extract_request_turns(req) == [("user", "real")]

    def test_empty_messages(self):
        assert extract_request_turns({}) == []
        assert extract_request_turns({"messages": []}) == []


# ---------------------------------------------------------------------------
# SSE parsing — sse_feed
# ---------------------------------------------------------------------------

class TestSseFeed:
    def test_single_event_single_chunk(self):
        state = SSEState()
        chunk = b"data: {\"delta\":{\"text\":\"hi\"}}\n\n"
        events = sse_feed(state, chunk)
        assert events == ["{\"delta\":{\"text\":\"hi\"}}"]

    def test_tcp_fragmentation(self):
        """SSE event split across two TCP chunks."""
        state = SSEState()
        chunk1 = b"data: {\"delta\":{\"text\":"
        chunk2 = b"\"hello\"}}\n\n"
        events1 = sse_feed(state, chunk1)
        events2 = sse_feed(state, chunk2)
        assert events1 == []
        assert events2 == ["{\"delta\":{\"text\":\"hello\"}}"]

    def test_multiple_events_in_one_chunk(self):
        state = SSEState()
        chunk = b"data: A\n\ndata: B\n\n"
        events = sse_feed(state, chunk)
        assert events == ["A", "B"]

    def test_done_event(self):
        state = SSEState()
        chunk = b"data: [DONE]\n\n"
        events = sse_feed(state, chunk)
        assert events == ["[DONE]"]

    def test_crlf_line_endings(self):
        state = SSEState()
        chunk = b"data: hello\r\n\r\n"
        events = sse_feed(state, chunk)
        assert events == ["hello"]

    def test_non_data_lines_ignored(self):
        state = SSEState()
        # event: and id: lines are not captured
        chunk = b"event: content_block_delta\ndata: hello\n\n"
        events = sse_feed(state, chunk)
        assert events == ["hello"]

    def test_partial_line_held_in_buffer(self):
        state = SSEState()
        chunk = b"data: partial"
        events = sse_feed(state, chunk)
        assert events == []
        assert state.buf == "data: partial"


# ---------------------------------------------------------------------------
# extract_text_delta
# ---------------------------------------------------------------------------

class TestExtractTextDelta:
    def test_delta_text(self):
        obj = {"delta": {"type": "text_delta", "text": "hello"}}
        assert extract_text_delta(obj) == "hello"

    def test_content_block_text(self):
        obj = {"content_block": {"type": "text", "text": "world"}}
        assert extract_text_delta(obj) == "world"

    def test_message_content_fallback(self):
        obj = {
            "message": {
                "content": [
                    {"type": "text", "text": "from message"},
                    {"type": "image"},
                ]
            }
        }
        assert extract_text_delta(obj) == "from message"

    def test_no_text_returns_empty(self):
        assert extract_text_delta({}) == ""
        assert extract_text_delta({"delta": {"type": "input_json_delta"}}) == ""

    def test_content_block_non_text_ignored(self):
        obj = {"content_block": {"type": "image"}}
        assert extract_text_delta(obj) == ""


# ---------------------------------------------------------------------------
# infer_session_id
# ---------------------------------------------------------------------------

class TestInferSessionId:
    def test_metadata_session_id_wins(self):
        req = {"metadata": {"session_id": "my-session"}}
        assert infer_session_id(req, "stanley") == "my-session"

    def test_fallback_to_hostname(self):
        assert infer_session_id({}, "stanley") == "stanley"
        assert infer_session_id({"metadata": {}}, "marlowe") == "marlowe"
        assert infer_session_id({"metadata": {"session_id": ""}}, "stanley") == "stanley"

    def test_fallback_unknown_when_no_hostname(self):
        assert infer_session_id({}, "") == "unknown"

    def test_none_metadata(self):
        req = {"metadata": None}
        assert infer_session_id(req, "stanley") == "stanley"


# ---------------------------------------------------------------------------
# extract_agent_id, extract_model
# ---------------------------------------------------------------------------

class TestExtractAgentId:
    def test_present(self):
        req = {"metadata": {"agent_id": "my-agent"}}
        assert extract_agent_id(req) == "my-agent"

    def test_absent(self):
        assert extract_agent_id({}) is None
        assert extract_agent_id({"metadata": {}}) is None


class TestExtractModel:
    def test_from_request(self):
        req = {"model": "claude-opus-4-6"}
        assert extract_model(req) == "claude-opus-4-6"

    def test_from_response(self):
        req = {}
        resp = {"model": "claude-sonnet-4-6"}
        assert extract_model(req, resp) == "claude-sonnet-4-6"

    def test_request_wins_over_response(self):
        req = {"model": "req-model"}
        resp = {"model": "resp-model"}
        assert extract_model(req, resp) == "req-model"

    def test_none_when_absent(self):
        assert extract_model({}) is None


# ---------------------------------------------------------------------------
# extract_assistant_text_from_response
# ---------------------------------------------------------------------------

class TestExtractAssistantText:
    def test_single_text_block(self):
        resp = {"content": [{"type": "text", "text": "Hi there"}]}
        assert extract_assistant_text_from_response(resp) == "Hi there"

    def test_multiple_text_blocks_concatenated(self):
        resp = {
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": " world"},
            ]
        }
        assert extract_assistant_text_from_response(resp) == "Hello world"

    def test_non_text_blocks_ignored(self):
        resp = {
            "content": [
                {"type": "tool_use", "id": "x"},
                {"type": "text", "text": "only this"},
            ]
        }
        assert extract_assistant_text_from_response(resp) == "only this"

    def test_empty_content(self):
        assert extract_assistant_text_from_response({"content": []}) == ""
        assert extract_assistant_text_from_response({}) == ""


# ---------------------------------------------------------------------------
# accumulate_sse_chunks (integration)
# ---------------------------------------------------------------------------

class TestAccumulateSseChunks:
    def test_accumulates_deltas(self):
        state = SSEState()
        parts: list[str] = []
        chunks = [
            b'data: {"delta":{"text":"He"}}\n\n',
            b'data: {"delta":{"text":"llo"}}\n\n',
            b"data: [DONE]\n\n",
        ]
        done = False
        for chunk in chunks:
            done = accumulate_sse_chunks(state, parts, chunk)
        assert "".join(parts) == "Hello"
        assert done is True

    def test_ignores_bad_json(self):
        state = SSEState()
        parts: list[str] = []
        chunk = b"data: not-json\n\ndata: {\"delta\":{\"text\":\"ok\"}}\n\n"
        accumulate_sse_chunks(state, parts, chunk)
        assert parts == ["ok"]

    def test_fragmented_chunks(self):
        state = SSEState()
        parts: list[str] = []
        # Split the SSE event across two byte chunks.
        chunk1 = b'data: {"delta":{"te'
        chunk2 = b'xt":"hi"}}\n\n'
        accumulate_sse_chunks(state, parts, chunk1)
        accumulate_sse_chunks(state, parts, chunk2)
        assert parts == ["hi"]
