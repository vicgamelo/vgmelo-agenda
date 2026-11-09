"""vgmelo-agenda: booking API, public MCP server and scheduling agent for vgmelo.tech."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .agent import AgentUnavailable, SchedulingAgent
from .api import AppContext, router
from .booking import Agenda, BookingError
from .calendar_feed import BusyFeed
from .config import Settings
from .db import make_engine, make_sessionmaker
from .mailer import Mailer
from .security import Keyring, TokenBucket, client_ip
from .tools import CLIENT_IP, build_mcp

logger = logging.getLogger("vgmelo_agenda")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MCP_PATH = "/api/agenda/mcp"


class Guard:
    """Outermost middleware: client IP, per-IP token buckets, Origin check on unsafe methods (CSRF and
    "use visitors' browsers to spread a flood over many IPs"), body cap, security headers, access log
    without query strings. Requests to MCP_PATH go to the MCP app; everything else to the API."""

    def __init__(self, api: ASGIApp, mcp: ASGIApp, settings: Settings) -> None:
        self.api, self.mcp, self.settings = api, mcp, settings
        self.api_bucket = TokenBucket(settings.api_rate_per_minute, max(10, int(settings.api_rate_per_minute // 2)))
        self.mcp_bucket = TokenBucket(settings.mcp_rate_per_minute, settings.mcp_burst)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.api(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        started = time.perf_counter()
        method, path = scope["method"], scope["path"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        client = scope.get("client")
        ip = client_ip(client[0] if client else None, headers.get("x-forwarded-for"), self.settings.proxies)
        scope["agenda.ip"] = ip
        CLIENT_IP.set(ip)
        is_mcp = path == MCP_PATH
        status = {"code": 0}

        async def send_wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"x-frame-options", b"DENY"),
                    (b"cache-control", b"no-store"),
                ]
                message["headers"] = [*message.get("headers", []), *(h for h in extra if h[0] not in existing)]
            await send(message)

        async def reply(code: int, detail: Any, retry_after: float | None = None) -> None:
            hdrs = {"Retry-After": str(max(1, int(retry_after) + 1))} if retry_after else None
            await JSONResponse({"detail": detail}, code, headers=hdrs)(scope, receive, send_wrapped)

        try:
            # Size first: an oversized request is refused without spending the caller's rate-limit tokens.
            declared = headers.get("content-length")
            limit = self.settings.max_body_bytes
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                await reply(413, {"code": "body_too_large"})
                return
            if path != "/api/agenda/health":
                wait = (self.mcp_bucket if is_mcp else self.api_bucket).hit(ip)
                if wait:
                    await reply(429, {"code": "rate_limited"}, wait)
                    return
            # MCP clients aren't browsers and send no Origin; the MCP transport checks Origin when present.
            origin = headers.get("origin", "").rstrip("/")
            if not is_mcp and method not in SAFE_METHODS and origin not in self.settings.origins:
                await reply(403, {"code": "bad_origin"})
                return
            body = b""
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body += message.get("body", b"")
                if len(body) > limit:
                    await reply(413, {"code": "body_too_large"})
                    return
                if not message.get("more_body", False):
                    break
            replayed = False

            async def replay() -> Message:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await (self.mcp if is_mcp else self.api)(scope, replay, send_wrapped)
        finally:
            logger.info("%s %s %s %.0fms", method, path, status["code"], (time.perf_counter() - started) * 1000)


def quiet_third_party_loggers() -> None:
    for noisy in ("httpx", "httpcore", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def create_app(
    settings: Settings | None = None,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ASGIApp:
    quiet_third_party_loggers()
    cfg = settings or Settings()  # type: ignore[call-arg]
    if not cfg.owner_email:
        logger.warning("OWNER_EMAIL is empty: confirmed bookings won't notify anyone")
    now = clock or (lambda: datetime.now(UTC))
    engine = make_engine(cfg.database_url)
    sessions = make_sessionmaker(engine)
    keyring = Keyring(cfg.secret_key)
    sync_http = httpx.Client(transport=transport, timeout=10)  # type: ignore[arg-type]
    async_http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10, read=30))  # type: ignore[arg-type]
    feed = BusyFeed(
        cfg.ics_url,
        cfg.tz,
        cfg.max_days_ahead,
        timedelta(hours=cfg.ics_max_stale_hours),
        timedelta(minutes=cfg.ics_refresh_minutes),
        async_http,
        now,
    )
    agenda = Agenda(cfg, sessions, Mailer(cfg.resend_api_key, cfg.email_from, sync_http), feed, keyring, now)
    agent = SchedulingAgent(agenda, keyring, async_http)
    ctx = AppContext(settings=cfg, agenda=agenda, agent=agent, keyring=keyring, feed=feed)

    mcp_app = build_mcp(agenda).streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=cfg.max_body_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(cfg.mcp_hosts),
            allowed_hosts=cfg.mcp_hosts,
            allowed_origins=sorted(cfg.origins),
        ),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        feed.start()
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
        await feed.stop()

    app = FastAPI(
        title="vgmelo-agenda", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.ctx = ctx
    app.state.engine = engine
    app.state.mcp = mcp_app

    @app.exception_handler(BookingError)
    async def booking_error(_: Request, exc: BookingError) -> JSONResponse:
        detail: dict[str, Any] = {"code": exc.code}
        headers = None
        if exc.retry_at is not None:
            detail["retry_at"] = exc.retry_at.isoformat(timespec="seconds")
            headers = {"Retry-After": str(max(1, int((exc.retry_at - datetime.now(UTC)).total_seconds())))}
        return JSONResponse({"detail": detail}, exc.status, headers=headers)

    @app.exception_handler(AgentUnavailable)
    async def agent_unavailable(_: Request, exc: AgentUnavailable) -> JSONResponse:
        code = 429 if exc.reason == "rate_limited" else 503
        return JSONResponse({"detail": {"code": f"agent_{exc.reason}"}}, code)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in e.get("loc", [])[1:]) for e in exc.errors()})
        return JSONResponse({"detail": {"code": "invalid", "fields": fields}}, 422)

    app.include_router(router)
    return Guard(app, mcp_app, cfg)


def main() -> None:
    import os

    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        "vgmelo_agenda.app:create_app",
        factory=True,
        host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104 - inside the container, behind Caddy
        port=int(os.getenv("PORT", "8000")),
        workers=1,  # conversations and token buckets are in-process
        proxy_headers=False,  # X-Forwarded-For is read by Guard, only from TRUSTED_PROXIES
        access_log=False,
        server_header=False,
    )
