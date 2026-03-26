# Stanley Logging Proxy — My Plan

## Context

Stanley (Raspberry Pi 5) runs OpenClaw as an AI gateway. Telegram exchanges are already logged via a native OpenClaw hook into the `exchanges` table. Dashboard and TUI surfaces have no equivalent capture. This proxy sits between OpenClaw and `api.anthropic.com`, intercepts `POST /v1/messages`, and logs conversation turns tagged `source='proxy'` into the same table.

---

## Module Structure

```
stanley-proxy/
├── config.py               # Frozen dataclass; env vars; fail-fast on missing DATABASE_URL
├── extract.py              # Pure functions: normalize content, parse SSE, build rows
├── db.py                   # asyncpg pool lifecycle + non-throwing log_exchange()
├── proxy.py                # aiohttp server, catch-all route, streaming tee
├── requirements.txt
├── stanley-proxy.service
└── README.md
```

**Key divergence from the existing design:** `logger.py` is split into `extract.py` + `db.py`.

- `extract.py` is pure Python (no async, no I/O) — unit-testable with zero infrastructure
- `db.py` owns only pool lifecycle and parameterized inserts

---

## config.py

```python
import os
import socket
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    proxy_host: str
    proxy_port: int
    upstream_url: str
    database_url: str
    log_level: str
    hostname: str           # socket.gethostname() at startup

def load_config() -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("DATABASE_URL is required")

    return Config(
        proxy_host=os.environ.get("PROXY_HOST", "127.0.0.1"),
        proxy_port=int(os.environ.get("PROXY_PORT", "8888")),
        upstream_url=os.environ.get("UPSTREAM_URL", "https://api.anthropic.com"),
        database_url=db_url,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        hostname=socket.gethostname(),
    )
```

`SystemExit` on missing `DATABASE_URL` — fail at startup, not at first DB write.

---

## extract.py (pure, no I/O)

```python
from dataclasses import dataclass, field
from typing import Any

def normalize_content(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return "\n".join(
            b["text"] for b in val
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""

def extract_request_turns(req_json: dict) -> list[tuple[str, str]]:
    turns = []
    for msg in req_json.get("messages", []):
        role = msg.get("role", "user")
        text = normalize_content(msg.get("content", ""))
        if text:
            turns.append((role, text))
    return turns

@dataclass
class SSEState:
    buf: str = ""
    data_lines: list[str] = field(default_factory=list)

def sse_feed(state: SSEState, chunk_bytes: bytes) -> list[str]:
    """Chunk-safe SSE line buffer. Returns list of complete event payloads."""
    out: list[str] = []
    state.buf += chunk_bytes.decode("utf-8", errors="replace")
    while True:
        nl = state.buf.find("\n")
        if nl == -1:
            break
        line = state.buf[:nl].rstrip("\r")
        state.buf = state.buf[nl + 1:]
        if line == "":
            if state.data_lines:
                out.append("\n".join(state.data_lines))
                state.data_lines.clear()
        elif line.startswith("data:"):
            payload = line[5:].lstrip(" ")
            state.data_lines.append(payload)
    return out

def extract_text_delta(event_obj: dict) -> str:
    """Three-tier fallback for Anthropic SSE event variants."""
    delta = event_obj.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return delta["text"]
    cb = event_obj.get("content_block")
    if isinstance(cb, dict) and cb.get("type") == "text" and isinstance(cb.get("text"), str):
        return cb["text"]
    msg = event_obj.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        return "".join(
            b["text"] for b in msg["content"]
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""

def infer_session_id(req_json: dict, hostname: str) -> str:
    """
    Inference chain:
    1. request_body["metadata"]["session_id"] if present and non-empty
    2. socket.gethostname() passed in as hostname
    3. 'unknown'
    """
    meta = req_json.get("metadata") or {}
    sid = meta.get("session_id", "")
    if sid:
        return sid
    if hostname:
        return hostname
    return "unknown"
```

### session_id Inference Chain

1. `request_body["metadata"]["session_id"]` if present and non-empty
2. `socket.gethostname()` captured at proxy startup (passed in as `hostname`)
3. `'unknown'`

**Rationale:** Stanley is headless (TUI only) → `'stanley'` identifies TUI origin exactly. Marlowe runs both dashboard and TUI → `'marlowe'` is ambiguous but machine-tagged, which is still more accurate than `'dashboard'` (which the existing design hard-codes). Zero-config; survives any future machine additions.

---

## db.py

```python
import asyncio
import logging
from typing import Optional
import asyncpg

logger = logging.getLogger(__name__)

class ExchangeLogger:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str, *, min_size: int = 1, max_size: int = 5) -> "ExchangeLogger":
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def log_exchange(
        self, *, session_id: str, agent_id: Optional[str], role: str,
        content: str, model: Optional[str]
    ) -> None:
        try:
            await self._pool.execute(
                """
                INSERT INTO exchanges (session_id, agent_id, role, content, model, source)
                VALUES ($1, $2, $3, $4, $5, 'proxy')
                """,
                session_id, agent_id, role, content, model,
            )
        except Exception:
            logger.exception("Failed to insert exchange row")
```

- Pool created in `app.on_startup`, closed in `app.on_cleanup`
- `log_exchange` swallows all exceptions, logs to stderr — logging never breaks proxying
- DB startup probe: `SELECT 1` with short timeout; log WARNING if fails, continue starting

---

## proxy.py

### Catch-all Route

```python
app.router.add_route("*", "/{tail:.*}", handle_proxy)
```

### Upstream Client

```python
aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
```

### Header Handling

Strip hop-by-hop headers on both request and response (`Connection`, `Keep-Alive`, `Transfer-Encoding`, etc.). Set `Host` to upstream host.

### Non-Streaming Path

1. Read full body → forward to upstream
2. Read full response body → return to client as `web.Response`
3. If `POST /v1/messages` + status 200: `asyncio.create_task(log_nonstream(...))`

### SSE Streaming Path (key divergence from existing design)

**Inline accumulator, no queue:**

```python
state = SSEState()
parts: list[str] = []

async for chunk in upstream_resp.content.iter_any():
    await stream_resp.write(chunk)          # always, unconditionally
    try:
        for payload in sse_feed(state, chunk):
            if payload == "[DONE]":
                continue
            import json
            obj = json.loads(payload)
            delta = extract_text_delta(obj)
            if delta:
                parts.append(delta)
    except Exception:
        logging.exception("SSE accumulation error")

# after loop:
asyncio.create_task(db.log_exchange(..., content="".join(parts)))
```

No queue. No concurrent background task during streaming. The accumulation is synchronous in-loop string ops (no I/O, cannot block event loop). Background task fires only after the stream is complete.

---

## SSE Architecture: Why Inline Accumulator

The existing design uses a queue-based tee with a concurrent background consumer task running during streaming.

**Why the queue is unnecessary here:**

- The background consumer does only string operations (`sse_feed`, `json.loads`, `str.join`) — no I/O
- String ops cannot block the event loop; they have negligible latency vs. network RTT
- Adding a queue introduces coordination overhead: queue sizing, consumer task lifecycle, race between stream completion and consumer readiness
- The simple inline pattern is easier to reason about and has identical correctness

**When a queue would be justified:** If the background task performed I/O _during_ streaming (e.g., per-chunk DB inserts). Here all DB work is deferred until after the stream ends, so the queue buys nothing.

---

## systemd Unit

```ini
[Unit]
Description=Stanley Anthropic Logging Proxy
Wants=network-online.target
After=network-online.target
Before=openclaw.service

[Service]
Type=simple
WorkingDirectory=/opt/stanley-proxy
EnvironmentFile=/etc/stanley-proxy/stanley-proxy.env
ExecStart=/opt/stanley-proxy/venv/bin/python /opt/stanley-proxy/proxy.py
Restart=on-failure
RestartSec=2
TimeoutStartSec=10
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log /tmp

[Install]
WantedBy=multi-user.target
```

---

## Verification Plan

| Layer | Method |
|---|---|
| 1 | `pytest` on `extract.py` pure functions (SSE fragmentation, content normalization, session_id chain) |
| 2 | `aiohttp.test_utils.TestServer` + real Postgres → assert row counts/content |
| 3 | Manual smoke test on Stanley — trigger dashboard/TUI request, query `source='proxy'` rows |
