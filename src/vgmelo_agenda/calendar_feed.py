"""Busy times from a published calendar (Outlook "Publish calendar" -> ICS link, availability only).

The feed is fetched in the background every ``ics_refresh_minutes``. Requests only read the last good copy.
If the copy gets older than ``ics_max_stale_hours`` (or there never was one), ``snapshot`` returns None and
no slots are offered: better a closed agenda than a call on top of something Victor already has.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events  # type: ignore[import-untyped]

from .rules import Interval

logger = logging.getLogger(__name__)

MAX_ICS_BYTES = 5 * 1024 * 1024


def _as_utc(value: date | datetime, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=tz)).astimezone(UTC)
    # All-day event: the whole local day(s).
    return datetime.combine(value, datetime.min.time(), tz).astimezone(UTC)


def _is_free(event: Any) -> bool:
    transp = str(event.get("TRANSP", "")).upper()
    busy_status = str(event.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper()
    status = str(event.get("STATUS", "")).upper()
    if status == "CANCELLED" or busy_status == "FREE":
        return True
    # Outlook marks "free" events TRANSPARENT; all-day events default to free there too.
    return transp == "TRANSPARENT" and busy_status not in {"BUSY", "OOF", "TENTATIVE", "WORKINGELSEWHERE"}


def parse_busy(ics: bytes | str, start: datetime, end: datetime, tz: ZoneInfo) -> list[Interval]:
    """Busy intervals overlapping [start, end), recurring events expanded."""
    cal = icalendar.Calendar.from_ical(ics)
    out: list[Interval] = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if _is_free(ev):
            continue
        dtstart = ev.get("DTSTART")
        if dtstart is None:
            continue
        s = dtstart.dt
        dtend = ev.get("DTEND")
        if dtend is not None:
            e = dtend.dt
        elif ev.get("DURATION") is not None:
            e = s + ev.get("DURATION").dt
        else:
            e = s + (timedelta(days=1) if not isinstance(s, datetime) else timedelta(0))
        a, b = _as_utc(s, tz), _as_utc(e, tz)
        if b > a:
            out.append(Interval(a, b))
    return sorted(out, key=lambda i: i.start)


class BusyFeed:
    def __init__(
        self,
        url: str,
        tz: ZoneInfo,
        horizon_days: int,
        max_stale: timedelta,
        refresh: timedelta,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime],
    ) -> None:
        self.url, self.tz, self.horizon_days = url, tz, horizon_days
        self.max_stale, self.refresh_every = max_stale, refresh
        self.client, self.clock = client, clock
        self.intervals: list[Interval] = []
        self.fetched_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def snapshot(self) -> list[Interval] | None:
        if not self.url:
            return []
        if self.fetched_at is None or self.clock() - self.fetched_at > self.max_stale:
            return None
        return self.intervals

    def load(self, ics: bytes | str) -> None:
        now = self.clock()
        self.intervals = parse_busy(ics, now - timedelta(days=1), now + timedelta(days=self.horizon_days + 2), self.tz)
        self.fetched_at = now

    async def refresh(self) -> bool:
        try:
            async with self.client.stream("GET", self.url, timeout=15, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    logger.warning("ics fetch: status %s", resp.status_code)
                    return False
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_ICS_BYTES:
                        logger.warning("ics fetch: feed larger than %s bytes", MAX_ICS_BYTES)
                        return False
            self.load(body)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ics fetch failed: %s", type(exc).__name__)
            return False
        logger.info("ics loaded: %s busy intervals", len(self.intervals))
        return True

    async def _loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_every.total_seconds())

    def start(self) -> None:
        if self.url and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
