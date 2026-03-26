# Design Comparison: My Plan vs. Stanley-Logging-Proxy.md

## Where Both Designs Agree

| Area | Both Designs |
|---|---|
| Language / runtime | Python + asyncio throughout |
| HTTP layer | `aiohttp` for both proxy server and upstream client |
| Database layer | `asyncpg` connection pool |
| Catch-all route | `/{tail:.*}` supporting all methods |
| Hop-by-hop stripping | Strip `Connection`, `Transfer-Encoding`, etc. on both request and response |
| Non-throwing inserts | `try/except` wrapping all DB calls; errors go to stderr only |
| Fire-and-forget DB work | `asyncio.create_task(...)` to keep DB writes off the proxy path |
| SSE detection | `Content-Type: text/event-stream` |
| Chunk-safe SSE parser | `SSEState` + `sse_feed` text buffer, line-by-line parsing |
| Delta extraction | Three-tier fallback: `delta.text` → `content_block.text` → `message.content[].text` |
| systemd ordering | `Before=openclaw.service`, `Restart=on-failure` |
| Configuration | Environment variables + optional `.env` file |
| `DATABASE_URL` validation | Raise/exit if missing |

---

## Divergences

### 1. SSE Architecture

| | Existing Design | My Design |
|---|---|---|
| **Approach** | Queue-based tee: background consumer task runs concurrently during streaming, consuming from an `asyncio.Queue` | Inline accumulator: string ops run synchronously inside the chunk loop; background task fires once after stream ends |
| **Concurrency during stream** | Two coroutines alive simultaneously: forwarder puts chunks on queue; consumer reads and parses | One coroutine; accumulation is pure string manipulation with negligible latency |
| **Queue sizing risk** | Queue can back up if consumer is slow (it won't be, but the risk exists in the model) | No queue; no sizing concern |
| **Complexity** | Higher: task creation, queue lifecycle, producer/consumer coordination | Lower: single loop, one background task at end |
| **Correctness** | Equal — same SSE parser, same DB insert | Equal |

**Verdict: My design wins.** The queue adds coordination complexity without any benefit. The consumer does no I/O during streaming — string ops are non-blocking. A queue is warranted when the consumer has genuine I/O latency; here it does not.

---

### 2. session_id Fallback

| | Existing Design | My Design |
|---|---|---|
| **Fallback value** | Hard-coded `'dashboard'` | `socket.gethostname()` (captured at startup), fallback `'unknown'` |
| **Stanley (headless, TUI only)** | Rows get `'dashboard'` — incorrect, TUI is not dashboard | Rows get `'stanley'` — accurate |
| **Marlowe (dashboard + TUI)** | Rows get `'dashboard'` — ambiguous but matches surface if only dashboard is used | Rows get `'marlowe'` — machine-tagged, honest about ambiguity |
| **Future machines** | Breaks: all machines get `'dashboard'` | Works: each machine self-identifies |
| **Config required** | None | None |

**Verdict: My design wins.** Hostname is more accurate, requires no per-machine configuration, and does not embed a false assumption about what surface generated the traffic. On Stanley specifically, `'stanley'` correctly identifies TUI; the existing design's `'dashboard'` is factually wrong.

Note: The design brief says "fall back to `'dashboard'`" — this was written with an ambiguous mental model of the deployment. Given the two-machine reality (Stanley=TUI only, Marlowe=both), hostname is strictly more accurate.

---

### 3. Module Split

| | Existing Design | My Design |
|---|---|---|
| **Extraction + DB** | Combined in `logger.py` | Split: `extract.py` (pure) + `db.py` (async) |
| **Testability** | Tests need asyncpg + DB or mocking | `extract.py` tests run with zero infrastructure |
| **Separation of concerns** | Moderate | Clear: pure functions vs. I/O |

**Verdict: My design wins modestly.** The split is not architecturally dramatic, but it makes the pure extraction functions easy to test and read independently. The existing design's `logger.py` is coherent and not wrong; the split is an improvement in testability, not a necessity.

---

### 4. DB Startup Probe

| | Existing Design | My Design |
|---|---|---|
| **Startup DB check** | None mentioned | `SELECT 1` with short timeout; log WARNING if fails, continue starting |

**Verdict: My design wins narrowly.** The probe surfaces DB connectivity issues at startup (visible in `journalctl`) rather than silently at the first request. It does not block startup — only warns.

---

## Summary Scorecard

| Dimension | Winner | Gap |
|---|---|---|
| SSE architecture | Mine | Significant (simpler, same correctness) |
| session_id fallback | Mine | Significant (accurate vs. wrong on Stanley) |
| Module split | Mine | Modest (testability) |
| DB startup probe | Mine | Modest (visibility) |
| SSE parser implementation | Tie | The reference implementation in the existing design is correct — reuse it |
| Everything else | Tie | Both designs agree |
