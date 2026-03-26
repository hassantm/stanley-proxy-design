# Stanley Logging Proxy — Design Brief

## Context

Stanley is a Raspberry Pi 5 running OpenClaw as an AI gateway. Chat exchanges originating from the **Telegram** surface are already captured via a native OpenClaw hook into a Postgres `exchanges` table. The **dashboard** and **TUI** surfaces have no equivalent capture mechanism. This project implements a lightweight HTTP proxy that sits between OpenClaw and `api.anthropic.com` to intercept and log those exchanges into the same table.

A feature request for native hook support has been filed with OpenClaw upstream. This proxy is the interim solution.

---

## Architecture

```
Dashboard ──┐
TUI ─────────┼──→ OpenClaw → stanley-proxy (localhost:8888) → api.anthropic.com
Telegram ────┘ (already logged via hook — do not double-capture)
```

---

## Requirements

### Functional

1. Intercept all HTTP requests OpenClaw makes to `api.anthropic.com`
2. Forward requests and responses transparently — OpenClaw must not be able to distinguish proxy from direct connection
3. On `/v1/messages` POST with HTTP 200 response, extract and log:
   - All messages in the request `messages` array (user/system turns)
   - The assistant response content blocks (text type only)
4. Tag all logged rows with `source = 'proxy'` to distinguish from Telegram hook rows
5. Do **not** break the proxy if logging fails — log the error to stderr and continue

### Non-Functional

- Python, using `aiohttp` for both the proxy server and upstream client
- Async throughout — no blocking calls in the request path
- Postgres connection via `asyncpg` connection pool
- Packaged as a `systemd` service unit that starts before OpenClaw and restarts on failure
- Must handle both streaming (`text/event-stream`) and non-streaming responses correctly

---

## Postgres Schema (existing — do not modify)

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

`agent_id` may not be available from the request — insert NULL if absent. `session_id` should be extracted from `request.metadata.session_id` if present; fall back to `'dashboard'` if not.

---

## Streaming Handling

OpenClaw may use `stream: true` on some requests. The proxy must:

1. Detect streaming responses via `Content-Type: text/event-stream`
2. Stream chunks through to the client **immediately** (do not buffer the full response before forwarding — this would break the UX)
3. Accumulate SSE `data:` chunks in a background task
4. On stream completion (final `data: [DONE]` chunk), assemble the full assistant message and insert to Postgres

---

## Configuration

All configuration via environment variables or a `.env` file:

| Variable | Default | Description |
|---|---|---|
| `PROXY_HOST` | `127.0.0.1` | Bind address |
| `PROXY_PORT` | `8888` | Bind port |
| `UPSTREAM_URL` | `https://api.anthropic.com` | Upstream base URL |
| `DATABASE_URL` | *(required)* | Postgres DSN |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## OpenClaw Integration

Document how to point OpenClaw's API base URL at the proxy. Expected options in order of preference:

1. OpenClaw config file — `api_base_url` or equivalent
2. Environment variable — `ANTHROPIC_BASE_URL` or `HTTPS_PROXY`
3. Fallback: note what to check in OpenClaw source

---

## Deliverables

```
stanley-proxy/
├── proxy.py               # Main aiohttp application
├── logger.py              # Exchange extraction and Postgres insert logic
├── config.py              # Environment variable loading
├── requirements.txt       # aiohttp, asyncpg, python-dotenv
├── stanley-proxy.service  # systemd unit file
└── README.md              # Setup, configuration, and OpenClaw integration notes
```

---

## Out of Scope

- Modifying the `exchanges` table schema
- Capturing non-`/v1/messages` endpoints (embeddings, etc.)
- TLS termination (Tailscale handles network security on Stanley)
- Deduplication with Telegram rows (handled by `source` column distinction)
