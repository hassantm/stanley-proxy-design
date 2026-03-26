"""
extract.py — pure functions, no async, no I/O.

All extraction, normalization, and SSE parsing lives here so it can be
unit-tested with zero infrastructure.
"""
import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Content normalization
# ---------------------------------------------------------------------------

def normalize_content(val: Any) -> str:
    """Normalize a messages[].content value to plain text.

    - str  → return as-is
    - list → join text blocks with newline, ignore non-text blocks
    - else → ''
    """
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts = [
            b["text"]
            for b in val
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


# ---------------------------------------------------------------------------
# Request turn extraction
# ---------------------------------------------------------------------------

def extract_request_turns(req_json: dict[str, Any]) -> list[tuple[str, str]]:
    """Return [(role, content_text), ...] for each message in req_json['messages'].

    Skips entries where normalized content is empty.
    """
    turns: list[tuple[str, str]] = []
    for msg in req_json.get("messages", []):
        role = msg.get("role", "user")
        text = normalize_content(msg.get("content", ""))
        if text:
            turns.append((role, text))
    return turns


def extract_assistant_text_from_response(resp_json: dict[str, Any]) -> str:
    """Concatenate text content blocks from a non-streaming response."""
    parts = [
        b["text"]
        for b in resp_json.get("content", [])
        if isinstance(b, dict)
        and b.get("type") == "text"
        and isinstance(b.get("text"), str)
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def infer_session_id(req_json: dict[str, Any], hostname: str) -> str:
    """Inference chain for session_id.

    1. request_body["metadata"]["session_id"] — explicit, most accurate
    2. hostname (socket.gethostname() captured at proxy startup)
    3. 'unknown'
    """
    meta = req_json.get("metadata") or {}
    sid = meta.get("session_id", "")
    if sid and isinstance(sid, str):
        return sid
    if hostname:
        return hostname
    return "unknown"


def extract_agent_id(req_json: dict[str, Any]) -> str | None:
    """Extract agent_id from metadata if present."""
    meta = req_json.get("metadata") or {}
    val = meta.get("agent_id")
    return str(val) if val else None


def extract_model(
    req_json: dict[str, Any],
    resp_json: dict[str, Any] | None = None,
) -> str | None:
    """Extract model: request field first, then response field, then None."""
    m = req_json.get("model")
    if m and isinstance(m, str):
        return m
    if resp_json:
        m = resp_json.get("model")
        if m and isinstance(m, str):
            return m
    return None


# ---------------------------------------------------------------------------
# SSE parsing (chunk-safe)
# ---------------------------------------------------------------------------

@dataclass
class SSEState:
    buf: str = ""
    data_lines: list[str] = field(default_factory=list)


def sse_feed(state: SSEState, chunk_bytes: bytes) -> list[str]:
    """Feed raw bytes into the SSE state machine.

    Returns a list of complete event payload strings (one per SSE event).
    Handles TCP fragmentation: partial lines remain in state.buf until a
    newline arrives.
    """
    out: list[str] = []
    state.buf += chunk_bytes.decode("utf-8", errors="replace")

    while True:
        nl = state.buf.find("\n")
        if nl == -1:
            break
        line = state.buf[:nl]
        state.buf = state.buf[nl + 1:]

        if line.endswith("\r"):
            line = line[:-1]

        if line == "":
            # Blank line terminates one SSE event.
            if state.data_lines:
                out.append("\n".join(state.data_lines))
                state.data_lines.clear()
            continue

        if line.startswith("data:"):
            payload = line[5:]
            if payload.startswith(" "):
                payload = payload[1:]
            state.data_lines.append(payload)

    return out


def extract_text_delta(event_obj: dict[str, Any]) -> str:
    """Best-effort text extraction from an Anthropic SSE event object.

    Three-tier fallback mirrors the Anthropic streaming API variants:
    1. event_obj["delta"]["text"]          — content_block_delta
    2. event_obj["content_block"]["text"]  — content_block_start with text
    3. event_obj["message"]["content"][i]["text"] — message_start with prefill
    """
    delta = event_obj.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return delta["text"]

    cb = event_obj.get("content_block")
    if isinstance(cb, dict) and cb.get("type") == "text" and isinstance(cb.get("text"), str):
        return cb["text"]

    msg = event_obj.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        parts = [
            b["text"]
            for b in msg["content"]
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        ]
        return "".join(parts)

    return ""


def accumulate_sse_chunks(
    state: SSEState,
    parts: list[str],
    chunk_bytes: bytes,
) -> bool:
    """Feed a chunk into the SSE state; append text deltas to parts.

    Returns True if [DONE] was seen (stream is complete).
    Ignores JSON parse errors — best-effort.
    """
    done = False
    for payload in sse_feed(state, chunk_bytes):
        if payload == "[DONE]":
            done = True
            continue
        try:
            obj = json.loads(payload)
            delta = extract_text_delta(obj)
            if delta:
                parts.append(delta)
        except (json.JSONDecodeError, TypeError):
            pass
    return done
