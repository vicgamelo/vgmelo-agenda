"""Web API under /api/agenda (same origin as the site; Caddy routes it here)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field

from . import rules
from .agent import SchedulingAgent
from .booking import Agenda, BookingError, BookingRequest
from .calendar_feed import BusyFeed
from .config import Settings
from .limits import LimitReached, Window, take
from .security import Keyring
from .tools import valid_tz_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agenda")


@dataclass
class AppContext:
    settings: Settings
    agenda: Agenda
    agent: SchedulingAgent
    keyring: Keyring
    feed: BusyFeed


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def get_ip(request: Request) -> str:
    return str(request.scope.get("agenda.ip", "unknown"))


Ctx = Annotated[AppContext, Depends(get_ctx)]
IP = Annotated[str, Depends(get_ip)]
Kind = Literal["job", "consulting"]
Lang = Literal["pt", "en"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BookingIn(Strict):
    kind: Kind
    start: AwareDatetime
    duration: int = Field(ge=15, le=90)
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=254)
    company: str | None = Field(default=None, max_length=100)
    topic: str = Field(min_length=3, max_length=1000)
    lang: Lang = "pt"
    tz: str | None = Field(default=None, max_length=64)
    website: str = Field(default="", max_length=200)  # honeypot: humans never see it
    started_at: int = Field(description="epoch milliseconds when the form was rendered")


class TokenIn(Strict):
    token: str = Field(min_length=20, max_length=100)


class RescheduleIn(TokenIn):
    start: AwareDatetime
    duration: int = Field(ge=15, le=90)


class ChatIn(Strict):
    conversation: str | None = Field(default=None, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    lang: Lang = "pt"
    tz: str | None = Field(default=None, max_length=64)


def _manage_limit(ctx: AppContext, ip: str) -> None:
    w = Window("manage_ip", 3600, ctx.settings.manage_max_per_ip_hour)
    with ctx.agenda.sessions() as db:
        try:
            take(db, w, ctx.keyring.mac(ip), time.time())
        except LimitReached as exc:
            raise BookingError("rate_limited", 429, exc.retry_at) from exc


@router.get("/health")
def health(ctx: Ctx) -> dict[str, str]:
    from . import __version__

    calendar = "off" if not ctx.feed.configured else ("ok" if ctx.agenda.calendar_ok() else "stale")
    return {"status": "ok", "version": __version__, "calendar": calendar}


@router.get("/config")
def config(ctx: Ctx) -> dict[str, Any]:
    s = ctx.settings
    return {
        "timezone": s.timezone,
        "kinds": {k: {"durations": rules.durations(s, k)} for k in rules.KINDS},
        "step_minutes": s.step_minutes,
        "min_notice_hours": s.min_notice_hours,
        "max_days_ahead": s.max_days_ahead,
        "max_per_day": s.max_per_day,
        "hold_minutes": s.hold_minutes,
        "change_cutoff_hours": s.change_cutoff_hours,
        "calendar_ok": ctx.agenda.calendar_ok(),
        "agent": ctx.agent.available(),
        "agent_max_chars": s.agent_message_max_chars,
        "mcp_url": f"{s.site_url}/api/agenda/mcp",
    }


@router.get("/slots")
def slots(ctx: Ctx, kind: Kind, duration: Annotated[int, Query(ge=15, le=90)]) -> dict[str, Any]:
    try:
        starts = ctx.agenda.free_starts(kind, duration)
    except BookingError as exc:
        if exc.code != "calendar_unavailable":
            raise
        return {"timezone": ctx.settings.timezone, "calendar_ok": False, "days": []}
    days: dict[str, list[str]] = {}
    for s in starts:
        days.setdefault(rules.local_day(ctx.settings, s), []).append(s.isoformat().replace("+00:00", "Z"))
    return {
        "timezone": ctx.settings.timezone,
        "calendar_ok": True,
        "days": [{"date": d, "starts": v} for d, v in days.items()],
    }


@router.post("/bookings", status_code=202)
def create_booking(body: BookingIn, ctx: Ctx, ip: IP) -> dict[str, Any]:
    elapsed = time.time() - body.started_at / 1000
    if body.website or not ctx.settings.min_fill_seconds <= elapsed <= 86400:
        # Same answer as a real booking, so bots don't learn which check caught them.
        logger.info("booking dropped (honeypot or timing)")
        return {"status": "pending", "hold_minutes": ctx.settings.hold_minutes}
    req = BookingRequest(
        kind=body.kind,
        start=body.start,
        duration=body.duration,
        name=body.name,
        email=str(body.email),
        topic=body.topic,
        company=body.company,
        lang=body.lang,
        guest_tz=valid_tz_name(body.tz),
    )
    ctx.agenda.create(req, ip, "web")
    return {"status": "pending", "hold_minutes": ctx.settings.hold_minutes}


@router.post("/booking")
def view_booking(body: TokenIn, ctx: Ctx, ip: IP) -> dict[str, Any]:
    _manage_limit(ctx, ip)
    return ctx.agenda.view(body.token)


@router.post("/booking/confirm")
def confirm_booking(body: TokenIn, ctx: Ctx, ip: IP) -> dict[str, str]:
    _manage_limit(ctx, ip)
    return {"status": ctx.agenda.confirm(body.token)}


@router.post("/booking/cancel")
def cancel_booking(body: TokenIn, ctx: Ctx, ip: IP) -> dict[str, str]:
    _manage_limit(ctx, ip)
    return {"status": ctx.agenda.cancel(body.token)}


@router.post("/booking/reschedule")
def reschedule_booking(body: RescheduleIn, ctx: Ctx, ip: IP) -> dict[str, str]:
    _manage_limit(ctx, ip)
    return {"status": ctx.agenda.reschedule(body.token, body.start, body.duration)}


@router.post("/chat")
async def chat(body: ChatIn, ctx: Ctx, ip: IP) -> dict[str, Any]:
    if len(body.message) > ctx.settings.agent_message_max_chars:
        raise BookingError("message_too_long", 422)
    tz = valid_tz_name(body.tz) or ctx.settings.timezone
    return await ctx.agent.reply(body.conversation, body.message, body.lang, tz, ip)
