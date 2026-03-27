# stanley-proxy

Lightweight async HTTP proxy that sits between OpenClaw and `api.anthropic.com`.
Intercepts `POST /v1/messages` and logs conversation turns to the `exchanges` Postgres table with `source='proxy'`.

---

## Requirements

- Python 3.11+
- Postgres (existing `exchanges` table)
- systemd (for service management)

---

## Configuration

All configuration via environment variables or a `.env` file in the working directory.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | *(required)* | Postgres DSN, e.g. `postgresql://user:pass@localhost/dbname` |
| `PROXY_HOST` | `127.0.0.1` | Bind address |
| `PROXY_PORT` | `8888` | Bind port |
| `UPSTREAM_URL` | `https://api.anthropic.com` | Anthropic API base URL |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Install

```bash
# On Stanley (Pi 5)
sudo mkdir -p /opt/stanley-proxy /etc/stanley-proxy
sudo cp -r . /opt/stanley-proxy/

cd /opt/stanley-proxy
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Create environment file
sudo tee /etc/stanley-proxy/stanley-proxy.env <<EOF
DATABASE_URL=postgresql://user:pass@localhost/stanley
PROXY_HOST=127.0.0.1
PROXY_PORT=8888
LOG_LEVEL=INFO
EOF
sudo chmod 600 /etc/stanley-proxy/stanley-proxy.env

# Install systemd unit
sudo cp stanley-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stanley-proxy
sudo systemctl start stanley-proxy
```

---

## Install (macOS — Marlowe)

> **Warning:** `DATABASE_URL` is stored in plaintext inside `com.stanley.proxy.plist`, which lives in your home directory. Edit it with appropriate care and ensure the file permissions are restrictive (`chmod 600 ~/Library/LaunchAgents/com.stanley.proxy.plist`).

```bash
sudo mkdir -p /opt/stanley-proxy
sudo cp -r . /opt/stanley-proxy/

cd /opt/stanley-proxy
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Edit DATABASE_URL in the plist before loading
nano com.stanley.proxy.plist

# Install and start
cp com.stanley.proxy.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stanley.proxy.plist
```

Check it's running:

```bash
launchctl list | grep stanley
tail -f /tmp/stanley-proxy.stderr.log
```

To stop/restart:

```bash
launchctl unload ~/Library/LaunchAgents/com.stanley.proxy.plist
launchctl load   ~/Library/LaunchAgents/com.stanley.proxy.plist
```

---

## OpenClaw Integration

Point OpenClaw's Anthropic API base URL at the proxy. Try in order:

**1. OpenClaw config file**

Look for `api_base_url`, `anthropic_base_url`, or a provider base URL setting:

```yaml
api_base_url: http://127.0.0.1:8888
```

**2. Environment variable**

If OpenClaw respects `ANTHROPIC_BASE_URL`:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8888
```

**3. HTTP proxy env var fallback**

If the Anthropic Python SDK honors proxy environment variables:

```bash
export HTTPS_PROXY=http://127.0.0.1:8888
```

**4. Source inspection fallback**

Search OpenClaw source for where `anthropic.Anthropic(...)` or `AsyncAnthropic(...)` is instantiated. Pass `base_url="http://127.0.0.1:8888"` there.

---

## Verification

After pointing OpenClaw at the proxy, trigger a dashboard or TUI request. Then:

```sql
SELECT session_id, role, left(content, 60), model, ts
FROM exchanges
WHERE source = 'proxy'
ORDER BY ts DESC
LIMIT 20;
```

Expected: rows for each request turn (user/system) plus one assistant row per response.

Verify streaming UX is unaffected — responses should stream at normal speed.

---

## Troubleshooting

**Proxy not receiving traffic**

Check OpenClaw is pointed at `http://127.0.0.1:8888` (not HTTPS). Verify with:
```bash
sudo journalctl -u stanley-proxy -f
```
A proxied request logs at DEBUG level.

**DB connection failed at startup**

The startup probe logs a WARNING but the proxy still starts. Inserts will fail silently until DB is reachable. Fix the DSN in `/etc/stanley-proxy/stanley-proxy.env` and restart.

**No rows with `source='proxy'`**

- Confirm the request hit the proxy (check logs)
- Confirm the upstream returned HTTP 200 (non-200 responses are not logged)
- Check for JSON parse errors in `journalctl`

**Streaming response appears buffered**

The proxy uses `web.StreamResponse` and `iter_any()` — chunks are forwarded immediately. If OpenClaw sees buffered responses, check for middleware or network buffering between OpenClaw and the proxy.

---

## Project Layout

```
stanley-proxy/
├── config.py               # Frozen dataclass, env loading, fail-fast validation
├── extract.py              # Pure functions: SSE parsing, content normalization, session_id
├── db.py                   # asyncpg pool + non-throwing log_exchange()
├── proxy.py                # aiohttp server + catch-all proxy route
├── tests/
│   └── test_extract.py     # Unit tests for extract.py (no infrastructure needed)
├── requirements.txt
├── stanley-proxy.service
└── README.md
```

---

## Running Tests

```bash
cd stanley-proxy
pip install pytest
pytest tests/
```

Tests cover: SSE fragmentation, content normalization, session_id inference chain, delta extraction, full accumulation pipeline. No database or network required.
