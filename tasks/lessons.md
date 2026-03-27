# Lessons

## 1. aiohttp ClientSession auto_decompress

**Rule:** Always set `auto_decompress=False` when using aiohttp as a transparent proxy.

**Why:** aiohttp decompresses the response body by default (`auto_decompress=True`), but does not strip `Content-Encoding` or recompute `Content-Length` from the upstream headers. The downstream client receives decompressed bytes with headers claiming gzip encoding — it tries to decompress already-decompressed content and gets garbage. Manifested as `200 0` in the access log and "terminated" in the TUI.

**How to apply:** Any time aiohttp `ClientSession` is used to forward bytes to a downstream client verbatim, set `auto_decompress=False` at session creation.

## 2. Read generated code before declaring done

**Rule:** Re-read every generated file before committing, not just syntax-check it.

**Why:** Two bugs in proxy.py survived to the initial commit: a dead `req_headers` block with a method reference assigned as a header value, and `aiohttp.client_reqrep.URL` (non-existent) instead of `yarl.URL`. Both were caught only on a manual re-read after the fact.

**How to apply:** After generating any non-trivial file, read it top to bottom before committing. Syntax checks (`ast.parse`) catch syntax errors but not logic errors or wrong API calls.
