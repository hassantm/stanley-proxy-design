# Recommendation: Implement My Plan

## Decision

Implement **my plan** (`docs/my-plan.md`), using the existing design (`Stanley-Logging-Proxy.md`) as a reference for the SSE parser and delta extractor implementations, which are correct and should be reused verbatim.

---

## Rationale

### 1. SSE inline accumulator is simpler and equally correct

The existing design's queue-based tee introduces two concurrent coroutines during streaming. The consumer does only string operations — no I/O. String ops are non-blocking and complete in microseconds vs. network RTTs in the hundreds of milliseconds. The queue adds coordination complexity (lifecycle, sizing, producer/consumer ordering) with zero correctness benefit.

My inline accumulator runs the same SSE parser and delta extractor in the same chunk loop that forwards bytes. A single background task fires once after the stream ends. It is easier to read, easier to debug, and harder to get wrong.

### 2. Hostname-based session_id is more accurate

The existing design falls back to `'dashboard'` when `metadata.session_id` is absent. On Stanley (headless, TUI only), this produces rows tagged `session_id='dashboard'` when the source is TUI — factually incorrect. The fallback is baked into the code, invisible in logs, and wrong for the primary machine.

My design falls back to `socket.gethostname()`, which produces `'stanley'` on Stanley and `'marlowe'` on Marlowe. This requires no configuration, is accurate for Stanley (TUI-only), and is honest about Marlowe's ambiguity rather than resolving it incorrectly.

### 3. extract.py pays dividends in testing

Isolating pure functions into `extract.py` means the most complex and fragile logic — SSE fragmentation handling, delta extraction, content normalization, session_id inference — can be exercised with `pytest` and zero infrastructure. No asyncpg, no mock pool, no test server. This matters during development on a Mac before deploying to Stanley, and it matters when debugging regressions months later.

### 4. DB startup probe surfaces problems early

A single `SELECT 1` probe at startup logs a warning if the database is unreachable. This is visible in `journalctl` immediately, rather than surfacing silently in per-request error logs much later.

---

## What to Borrow from the Existing Design

The existing design's SSE implementation (Section 10) is correct and well-specified. Copy it verbatim:

- `SSEState` dataclass and `sse_feed()` function
- `extract_text_delta_from_anthropic_sse()` three-tier fallback

These are the most subtle parts of the implementation. There is no reason to rewrite them.

---

## Implementation Order

1. `extract.py` + `tests/test_extract.py` — pure functions, no infrastructure, fast feedback
2. `config.py` + `db.py` — test pool lifecycle against real Postgres
3. `proxy.py` transparent forwarding — no logging yet, verify passthrough correctness
4. Non-streaming logging path
5. Streaming logging path
6. systemd unit + README + deploy to Stanley
