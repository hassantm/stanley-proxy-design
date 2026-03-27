"""
proxy.py — aiohttp reverse proxy with exchange logging.

Intercepts all traffic from OpenClaw to api.anthropic.com.
Logs POST /v1/messages exchanges (streaming and non-streaming) to Postgres.
"""
import asyncio
import json
import logging
import sys
from typing import Any

import aiohttp
from aiohttp import web
from yarl import URL as YarlURL

from config import Config, load_config
from db import ExchangeLogger
from extract import (
    SSEState,
    accumulate_sse_chunks,
    extract_agent_id,
    extract_assistant_text_from_response,
    extract_model,
    extract_request_turns,
    infer_session_id,
)

logger = logging.getLogger(__name__)

# Hop-by-hop headers that must not be forwarded.
_HOP_BY_HOP = frozenset(
    h.lower()
    for h in (
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
    )
)


def _strip_hop_by_hop(headers: Any) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _is_messages_endpoint(request: web.Request) -> bool:
    return request.method == "POST" and request.path == "/v1/messages"


def _is_event_stream(upstream_resp: aiohttp.ClientResponse) -> bool:
    ct = upstream_resp.headers.get("Content-Type", "")
    return ct.startswith("text/event-stream")


async def _log_nonstream(
    db: ExchangeLogger,
    req_json: dict,
    resp_json: dict,
    hostname: str,
) -> None:
    session_id = infer_session_id(req_json, hostname)
    agent_id = extract_agent_id(req_json)
    model = extract_model(req_json, resp_json)
    turns = extract_request_turns(req_json)
    assistant_text = extract_assistant_text_from_response(resp_json)

    await db.log_turns(
        session_id=session_id,
        agent_id=agent_id,
        model=model,
        turns=turns,
    )
    if assistant_text:
        await db.log_exchange(
            session_id=session_id,
            agent_id=agent_id,
            role="assistant",
            content=assistant_text,
            model=model,
        )


async def _log_stream_final(
    db: ExchangeLogger,
    req_json: dict,
    assistant_text: str,
    hostname: str,
    resp_model: str | None,
) -> None:
    session_id = infer_session_id(req_json, hostname)
    agent_id = extract_agent_id(req_json)
    model = extract_model(req_json) or resp_model
    turns = extract_request_turns(req_json)

    await db.log_turns(
        session_id=session_id,
        agent_id=agent_id,
        model=model,
        turns=turns,
    )
    if assistant_text:
        await db.log_exchange(
            session_id=session_id,
            agent_id=agent_id,
            role="assistant",
            content=assistant_text,
            model=model,
        )


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    cfg: Config = request.app["config"]
    db: ExchangeLogger = request.app["db"]
    session: aiohttp.ClientSession = request.app["session"]

    upstream_url = cfg.upstream_url + request.path_qs

    req_body = await request.read()

    # Build upstream headers: strip hop-by-hop, fix Host.
    fwd_headers = _strip_hop_by_hop(request.headers)
    fwd_headers["Host"] = YarlURL(cfg.upstream_url).host or "api.anthropic.com"

    # Parse request JSON if this is the logging endpoint.
    req_json: dict | None = None
    if _is_messages_endpoint(request) and req_body:
        try:
            req_json = json.loads(req_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Could not parse request body as JSON")

    async with session.request(
        method=request.method,
        url=upstream_url,
        headers=fwd_headers,
        data=req_body,
        allow_redirects=False,
    ) as upstream_resp:

        resp_headers = _strip_hop_by_hop(upstream_resp.headers)
        should_log = (
            req_json is not None
            and upstream_resp.status == 200
        )

        # --- Streaming path ---
        if _is_event_stream(upstream_resp):
            stream_resp = web.StreamResponse(
                status=upstream_resp.status,
                headers=resp_headers,
            )
            await stream_resp.prepare(request)

            sse_state = SSEState()
            parts: list[str] = []
            resp_model: str | None = None

            try:
                async for chunk in upstream_resp.content.iter_any():
                    await stream_resp.write(chunk)
                    if should_log:
                        try:
                            accumulate_sse_chunks(sse_state, parts, chunk)
                        except Exception:
                            logger.exception("SSE accumulation error (non-fatal)")
                await stream_resp.write_eof()
            except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                logger.debug("Client disconnected during streaming")

            if should_log and req_json is not None:
                assistant_text = "".join(parts)
                asyncio.create_task(
                    _log_stream_final(db, req_json, assistant_text, cfg.hostname, resp_model)
                )

            return stream_resp

        # --- Non-streaming path ---
        resp_body = await upstream_resp.read()

        if should_log and req_json is not None:
            resp_json: dict | None = None
            try:
                resp_json = json.loads(resp_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.debug("Could not parse response body as JSON")

            if resp_json is not None:
                asyncio.create_task(
                    _log_nonstream(db, req_json, resp_json, cfg.hostname)
                )

        return web.Response(
            status=upstream_resp.status,
            headers=resp_headers,
            body=resp_body,
        )


async def create_app(cfg: Config) -> web.Application:
    app = web.Application()
    app["config"] = cfg

    async def on_startup(app: web.Application) -> None:
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        app["session"] = aiohttp.ClientSession(connector=connector)

        db = await ExchangeLogger.create(cfg.database_url)
        await db.probe()
        app["db"] = db
        logger.info(
            "Stanley proxy starting on %s:%d → %s",
            cfg.proxy_host,
            cfg.proxy_port,
            cfg.upstream_url,
        )

    async def on_cleanup(app: web.Application) -> None:
        await app["session"].close()
        await app["db"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/{tail:.*}", handle_proxy)
    return app


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    async def _run() -> None:
        app = await create_app(cfg)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, cfg.proxy_host, cfg.proxy_port)
        await site.start()
        logger.info("Listening on %s:%d", cfg.proxy_host, cfg.proxy_port)
        # Run forever.
        await asyncio.Event().wait()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
