## Implementation Report (Design Spec + Checklist) ChatG
## Purpose:

Implement a lightweight local HTTP proxy on Stanley (Raspberry Pi 5) that sits between OpenClaw and `api.anthropic.com`, intercepts `POST /v1/messages`, and logs dashboard/TUI exchanges into the existing Postgres `exchanges` table with `source='proxy'`. Telegram traffic is already captured upstream and should remain distinct via `source`.

---

## 1. Scope and Constraints

### In Scope
- Intercept *all* HTTP requests OpenClaw makes to the Anthropic upstream (default `https://api.anthropic.com`).
- Transparently forward requests/responses so OpenClaw cannot distinguish proxy vs direct connection.
- Log only:
  - `POST /v1/messages` requests that result in **HTTP 200** responses.
  - Request-side message turns from `request.messages[]` (user/system).
  - Response-side assistant message from **text content blocks only**.
- Streaming and non-streaming response support.
- Python, **asyncio throughout**, `aiohttp` for server and upstream client, `asyncpg` pool for Postgres.
- Systemd service, starts before OpenClaw, restarts on failure.
- Configuration via env vars or `.env`.

### Out of Scope
- Modifying Postgres schema.
- Capturing endpoints other than `/v1/messages`.
- TLS termination.
- Deduplication with Telegram beyond `source` tagging.

---

## 2. Data Model (Existing Postgres Schema)

Do **not** modify the table.

```sql
exchanges (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT,
    agent_id    TEXT,
    role        TEXT,       -- 'user' | 'assistant' | 'system'
    content     TEXT,
    model       TEXT,
    source      TEXT,       -- 'telegram' | 'proxy'
    ts          TIMESTAMPTZ DEFAULT NOW()
)
```
```

### Logging Rules
- `source`: always `'proxy'`
- `session_id`: `request.metadata.session_id` if present, else `'dashboard'`
- `agent_id`: from request metadata if present; else `NULL`
- `model`: from request `model` if present; else from response if present; else `NULL`
- Insert rows:
  1. For each message in `request.messages[]`: insert one row `(role, content_text)`
  2. For the assistant response: insert **one row** `role='assistant'` with concatenated **text-only** response blocks.

---

## 3. System Architecture

```
Dashboard ──┐
TUI ─────────┼──→ OpenClaw → stanley-proxy (127.0.0.1:8888) → api.anthropic.com
Telegram ────┘ (already logged via hook — do not modify; distinct via source)
```

Proxy is a local HTTP server. OpenClaw’s Anthropic base URL is set to the proxy.

---

## 4. Project Layout (Deliverables)

```
stanley-proxy/
├── proxy.py               # Main aiohttp application
├── logger.py              # Exchange extraction and Postgres insert logic
├── config.py              # Environment variable loading
├── requirements.txt       # aiohttp, asyncpg, python-dotenv
├── stanley-proxy.service  # systemd unit file
└── README.md              # Setup, configuration, OpenClaw integration notes
```

---

## 5. Configuration

Load from environment variables and/or `.env` file:

| Variable | Default | Description |
|---|---:|---|
| `PROXY_HOST` | `127.0.0.1` | Bind address |
| `PROXY_PORT` | `8888` | Bind port |
| `UPSTREAM_URL` | `https://api.anthropic.com` | Upstream base URL |
| `DATABASE_URL` | *(required)* | Postgres DSN |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## 6. HTTP Forwarding Spec (Transparency)

### 6.1 Catch-all Routing
- Use a single catch-all handler for `/{tail:.*}` supporting all methods.
- Build upstream URL as: `UPSTREAM_URL + request.path_qs`

### 6.2 Header Handling
To avoid proxy artifacts and comply with HTTP semantics:
- Strip hop-by-hop headers on both request and response:
  - `Connection`, `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Authorization`, `TE`,
    `Trailer`, `Transfer-Encoding`, `Upgrade`
- Do not forward `Host` as-is; let the upstream client set it (or set it to upstream host).

### 6.3 Body Handling
- Read request body **once** into bytes:
  - Forward the same bytes upstream.
  - For `/v1/messages`, best-effort parse JSON from those bytes for logging.
- For non-streaming responses:
  - Read response body bytes fully.
  - Return exactly those bytes to the client.
  - If eligible for logging, parse JSON from those bytes (best-effort) and log in background.

---

## 7. Logging Spec

### 7.1 Eligible Requests
Log only if all are true:
- request method is `POST`
- request path is exactly `/v1/messages`
- upstream response status is `200`
- request/response JSON parsing succeeds sufficiently to extract messages/text (best-effort)

### 7.2 Request Message Extraction
From `request_json["messages"]`:
- Each element has:
  - `role`: `'user' | 'assistant' | 'system'` (request should normally include user/system; but log what is present)
  - `content`: may be a string or array of blocks

Normalize to plain text:
- if `content` is string → use it
- if `content` is list → join only blocks where `block.type == "text"` using `"\n"`
- ignore non-text blocks

### 7.3 Response Assistant Extraction (non-streaming)
From `response_json["content"]` (typically list of blocks):
- take blocks where `type == "text"`
- concatenate `text` into a single assistant message
- insert **one** assistant row

### 7.4 Failure Handling
- Logging failures must never break proxying:
  - wrap DB logging calls in `try/except`
  - log exceptions to stderr via standard Python logging
- Never perform blocking calls in request path.
- Prefer `asyncio.create_task(...)` to offload DB work from proxy handler.

---

## 8. Streaming (SSE) Handling Spec

### 8.1 Detection
Treat as streaming if upstream response header indicates:
- `Content-Type` starts with `text/event-stream`

### 8.2 Pass-through Requirements
- Stream chunks to OpenClaw **immediately**; do not buffer the whole response.
- Use `aiohttp.web.StreamResponse`.

### 8.3 Background Accumulation Requirements
- Accumulate SSE `data:` payloads in a background task.
- When the final `data: [DONE]` event is observed:
  - assemble full assistant text from incremental deltas
  - insert rows (request turns + final assistant row)

### 8.4 SSE Parsing Approach (Chunk-safe)
- Maintain a text buffer for partial lines across chunks.
- Parse line-by-line:
  - blank line terminates one SSE event
  - collect `data:` lines for that event
  - yield one payload string per event (joined by `\n`)
- For each event payload:
  - if payload is `[DONE]` → finalize
  - else parse JSON; extract any assistant text delta best-effort

### 8.5 Robust Delta Extraction (Best-effort)
Handle common Anthropic event variants by checking:
- `event_obj["delta"]["text"]`
- `event_obj["content_block"]["text"]` when `content_block.type == "text"`
- fallback: `event_obj["message"]["content"][i].text` for `type=="text"`

If no text is found for an event, ignore it.

---

## 9. Module Responsibilities and Recommended APIs

### 9.1 `config.py`
**Responsibilities**
- Load `.env` if present.
- Validate `DATABASE_URL`.
- Provide typed `Config`.

**Recommended API**
```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    proxy_host: str
    proxy_port: int
    upstream_url: str
    database_url: str
    log_level: str

def load_config() -> Config:
    ...
```

### 9.2 `logger.py`
**Responsibilities**
- Own asyncpg pool creation/closure.
- Provide non-throwing, best-effort insertion methods.
- Normalize content and build rows.

**Recommended API**
```python
import asyncpg
from dataclasses import dataclass
from typing import Any, Optional

@dataclass(frozen=True)
class ExchangeRow:
    session_id: str
    agent_id: Optional[str]
    role: str
    content: str
    model: Optional[str]
    source: str  # 'proxy'

class ExchangeLogger:
    @classmethod
    async def create(cls, dsn: str, *, min_size: int = 1, max_size: int = 5) -> "ExchangeLogger": ...
    async def close(self) -> None: ...

    async def log_nonstream(self, *, request_json: dict[str, Any], response_json: dict[str, Any]) -> None: ...
    async def log_stream_final(self, *, request_json: dict[str, Any], assistant_text: str, response_model: Optional[str] = None) -> None: ...

def extract_session_id(request_json: dict[str, Any]) -> str: ...
def extract_agent_id(request_json: dict[str, Any]) -> Optional[str]: ...
def extract_model(request_json: dict[str, Any], response_json: Optional[dict[str, Any]] = None) -> Optional[str]: ...
def normalize_message_text(content: Any) -> str: ...
def extract_request_turns(request_json: dict[str, Any]) -> list[tuple[str, str]]: ...
def extract_assistant_text_from_response(response_json: dict[str, Any]) -> str: ...
```

### 9.3 `proxy.py`
**Responsibilities**
- aiohttp server + routes.
- Upstream client session.
- Special-case `/v1/messages` for logging.
- Streaming tee + SSE background accumulation.

**Recommended API**
```python
from aiohttp import web

async def create_app() -> web.Application: ...
async def handle_proxy(request: web.Request) -> web.StreamResponse: ...

def is_messages_endpoint(request: web.Request) -> bool: ...
def is_event_stream_response(upstream_resp) -> bool: ...
def strip_hop_by_hop_headers(headers) -> dict[str, str]: ...
```

---

## 10. SSE Parser (Reference Implementation Sketch)

Use this exact *pattern* (chunk-safe, event-based):

```python
from dataclasses import dataclass

@dataclass
class SSEState:
    buf: str = ""
    data_lines: list[str] | None = None

    def __post_init__(self) -> None:
        if self.data_lines is None:
            self.data_lines = []

def sse_feed(state: SSEState, chunk_text: str) -> list[str]:
    out: list[str] = []
    state.buf += chunk_text

    while True:
        nl = state.buf.find("\n")
        if nl == -1:
            break

        line = state.buf[:nl]
        state.buf = state.buf[nl + 1 :]

        if line.endswith("\r"):
            line = line[:-1]

        if line == "":
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
```

And a best-effort extractor:

```python
import json
from typing import Any

def extract_text_delta_from_anthropic_sse(event_obj: dict[str, Any]) -> str:
    delta = event_obj.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return delta["text"]

    cb = event_obj.get("content_block")
    if isinstance(cb, dict) and cb.get("type") == "text" and isinstance(cb.get("text"), str):
        return cb["text"]

    msg = event_obj.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        parts: list[str] = []
        for b in msg["content"]:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
        return "".join(parts)

    return ""
```

---

## 11. systemd Unit Spec (`stanley-proxy.service`)

**Goals**
- Starts before OpenClaw.
- Restarts on failure.
- Loads environment variables from a file.
- Runs under a dedicated user if possible.

**Template (adjust paths and OpenClaw unit name):**
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
# Optional hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log /tmp

[Install]
WantedBy=multi-user.target
```

---

## 12. OpenClaw Integration Notes (README Content)

Document how to point OpenClaw to the proxy in this order:

1. **OpenClaw config file**
   - Look for `api_base_url`, `anthropic_base_url`, or provider base URL setting.
   - Set it to: `http://127.0.0.1:8888`

2. **Environment variable**
   - If supported: `ANTHROPIC_BASE_URL=http://127.0.0.1:8888`

3. **Proxy env var fallback**
   - `HTTPS_PROXY=http://127.0.0.1:8888`
   - Note: only works if the Anthropic client honors proxy env variables.

4. **Source inspection fallback**
   - Search OpenClaw for where the Anthropic client is instantiated and whether base URL is configurable.

**Verification**
- Trigger a dashboard or TUI request.
- Confirm UX unaffected (especially streaming).
- Confirm `exchanges` has new rows with `source='proxy'` and expected `session_id`.

---

## 13. Implementation Checklist (Developer Task List)

### Milestone 0 — Skeleton
- [ ] Create `stanley-proxy/` directory structure
- [ ] Add `requirements.txt`: `aiohttp`, `asyncpg`, `python-dotenv`
- [ ] Add basic logging configuration (stderr handler, level via `LOG_LEVEL`)

### Milestone 1 — Config & lifecycle
- [ ] Implement `load_config()` with `.env` support and validation
- [ ] In `proxy.py`, create `aiohttp.web.Application`
- [ ] Add startup hooks:
  - [ ] create `aiohttp.ClientSession`
  - [ ] create `asyncpg` pool via `ExchangeLogger.create()`
- [ ] Add cleanup hooks to close both

### Milestone 2 — Transparent proxying
- [ ] Implement catch-all route `/{tail:.*}`
- [ ] Implement `strip_hop_by_hop_headers()` for request and response
- [ ] Forward method/path/query/headers/body to `UPSTREAM_URL`
- [ ] Return upstream status/headers/body unchanged

### Milestone 3 — Non-stream `/v1/messages` logging
- [ ] Detect `POST /v1/messages`
- [ ] Parse request JSON (best-effort) without breaking forwarding
- [ ] For upstream 200 with JSON response:
  - [ ] parse response JSON (best-effort)
  - [ ] schedule `ExchangeLogger.log_nonstream(...)` via `asyncio.create_task`
- [ ] Ensure logger never raises into the request handler

### Milestone 4 — Streaming support
- [ ] Detect `text/event-stream`
- [ ] Implement `web.StreamResponse` passthrough without buffering
- [ ] Implement SSE accumulation in a **background task** using queue + `sse_feed`
- [ ] On `[DONE]`, assemble assistant text and log via `log_stream_final(...)`
- [ ] Ensure SSE parsing errors do not impact streaming passthrough

### Milestone 5 — systemd + docs
- [ ] Write `stanley-proxy.service`
- [ ] Write `README.md` with:
  - [ ] install/venv steps
  - [ ] env var table
  - [ ] OpenClaw integration
  - [ ] troubleshooting (DB down, proxy not hit, streaming issues)
- [ ] Deploy to `/opt/stanley-proxy`, create env file under `/etc/stanley-proxy/`

### Milestone 6 — Test plan (recommended)
- [ ] Implement a mock upstream aiohttp server for:
  - [ ] non-stream JSON response
  - [ ] SSE streaming ending in `[DONE]`
- [ ] Verify:
  - [ ] proxy forwarding correctness
  - [ ] inserts for request turns + single assistant row
  - [ ] DB outage does not break proxy behavior

---

## 14. Acceptance Criteria

- OpenClaw works identically with proxy enabled (including streaming UX).
- For each `POST /v1/messages` with HTTP 200:
  - Inserts request turns (user/system) into `exchanges`.
  - Inserts exactly one assistant row with concatenated text content blocks.
  - All inserted rows have `source='proxy'`.
  - `session_id` extraction rule is respected.
- Logging failures never impact proxying; errors only appear on stderr logs.
- Runs reliably under systemd and starts before OpenClaw.

---
```