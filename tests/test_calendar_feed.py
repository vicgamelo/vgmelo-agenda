from __future__ import annotations

import asyncio
from datetime import UTC, timedelta

import httpx

from tests.conftest import BRT, ICS_URL, T0, Clock, EnvFactory, FakeNet, brt
from vgmelo_agenda.calendar_feed import BusyFeed, parse_busy

# Trimmed from a real Outlook "publish calendar" feed: Windows TZID, recurring event, free/OOF/cancelled.
OUTLOOK_ICS = """\
BEGIN:VCALENDAR
METHOD:PUBLISH
PRODID:Microsoft Exchange Server 2010
VERSION:2.0
BEGIN:VTIMEZONE
TZID:E. South America Standard Time
BEGIN:STANDARD
DTSTART:16010101T000000
TZOFFSETFROM:-0300
TZOFFSETTO:-0300
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:busy-1
SUMMARY:Ocupado
DTSTART;TZID=E. South America Standard Time:20261110T083000
DTEND;TZID=E. South America Standard Time:20261110T090000
TRANSP:OPAQUE
X-MICROSOFT-CDO-BUSYSTATUS:BUSY
END:VEVENT
BEGIN:VEVENT
UID:weekly
SUMMARY:Ocupado
RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=4
DTSTART;TZID=E. South America Standard Time:20261104T120000
DTEND;TZID=E. South America Standard Time:20261104T130000
X-MICROSOFT-CDO-BUSYSTATUS:BUSY
END:VEVENT
BEGIN:VEVENT
UID:free
SUMMARY:Livre
DTSTART;TZID=E. South America Standard Time:20261110T180000
DTEND;TZID=E. South America Standard Time:20261110T190000
TRANSP:TRANSPARENT
X-MICROSOFT-CDO-BUSYSTATUS:FREE
END:VEVENT
BEGIN:VEVENT
UID:oof
SUMMARY:Fora do escritório
DTSTART;VALUE=DATE:20261112
DTEND;VALUE=DATE:20261113
TRANSP:TRANSPARENT
X-MICROSOFT-CDO-BUSYSTATUS:OOF
END:VEVENT
BEGIN:VEVENT
UID:holiday
SUMMARY:Feriado
DTSTART;VALUE=DATE:20261113
DTEND;VALUE=DATE:20261114
TRANSP:TRANSPARENT
X-MICROSOFT-CDO-BUSYSTATUS:FREE
END:VEVENT
BEGIN:VEVENT
UID:cancelled
SUMMARY:Cancelado
STATUS:CANCELLED
DTSTART:20261114T110000Z
DTEND:20261114T120000Z
END:VEVENT
BEGIN:VEVENT
UID:utc
SUMMARY:Ocupado
DTSTART:20261116T210000Z
DTEND:20261116T220000Z
END:VEVENT
END:VCALENDAR
"""


def local_pairs(ics: str) -> list[tuple[str, str]]:
    out = parse_busy(ics, T0.astimezone(UTC), (T0 + timedelta(days=15)).astimezone(UTC), BRT)
    return [(i.start.astimezone(BRT).strftime("%d %H:%M"), i.end.astimezone(BRT).strftime("%d %H:%M")) for i in out]


def test_parse_outlook_feed() -> None:
    assert local_pairs(OUTLOOK_ICS) == [
        ("10 08:30", "10 09:00"),  # Windows TZID resolved to -03:00
        ("11 12:00", "11 13:00"),  # weekly, expanded
        ("12 00:00", "13 00:00"),  # all-day out of office blocks the whole day
        ("16 18:00", "16 19:00"),  # UTC event in local time
        ("18 12:00", "18 13:00"),  # 04/11 is in the past and 25/11 is past the window asked for
    ]


def test_busy_times_block_slots_through_the_api(make_env: EnvFactory) -> None:
    env = make_env(ics_url=ICS_URL)
    env.ctx.feed.load(OUTLOOK_ICS)
    starts = env.slots()
    as_local = {s: None for s in starts}

    def has(dt: object) -> bool:
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") in as_local  # type: ignore[attr-defined]

    assert has(brt(10, 8, 0)) is False  # 08:00-08:30 touches the 08:30 event with no buffer
    assert has(brt(10, 9, 15)) is True
    assert has(brt(10, 18, 0)) is True  # "free" event doesn't block
    assert not [s for s in starts if s.startswith("2026-11-12")]  # OOF day
    assert has(brt(13, 8, 0)) is True  # holiday marked free
    r = env.book(brt(10, 8, 0))
    assert r.status_code == 409


def test_stale_feed_closes_the_agenda(make_env: EnvFactory, clock: Clock) -> None:
    env = make_env(ics_url=ICS_URL, ics_max_stale_hours=6)
    env.ctx.feed.load(OUTLOOK_ICS)
    assert env.client.get("/api/agenda/config").json()["calendar_ok"] is True
    clock.advance(hours=6, minutes=1)
    r = env.client.get("/api/agenda/slots", params={"kind": "job", "duration": 30}).json()
    assert r == {"timezone": "America/Sao_Paulo", "calendar_ok": False, "days": []}
    b = env.book(brt(11, 8, 0))
    assert b.status_code == 503 and b.json()["detail"]["code"] == "calendar_unavailable"
    assert env.client.get("/api/agenda/health").json()["calendar"] == "stale"


def test_no_feed_configured_means_only_our_bookings_count(env: object) -> None:
    assert env.client.get("/api/agenda/health").json()["calendar"] == "off"  # type: ignore[attr-defined]


def test_refresh_keeps_last_good_copy_on_errors(net: FakeNet, clock: Clock) -> None:
    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(net.handler))
        feed = BusyFeed(ICS_URL, BRT, 14, timedelta(hours=6), timedelta(minutes=10), http, clock)
        assert feed.snapshot() is None  # configured but never fetched: closed
        net.ics = OUTLOOK_ICS
        assert await feed.refresh()
        assert feed.snapshot() and len(feed.snapshot() or []) == 5
        net.ics_status = 500
        clock.advance(hours=1)
        assert not await feed.refresh()
        assert len(feed.snapshot() or []) == 5  # last good copy still used
        net.ics_status, net.ics = 200, "not an ics"
        assert not await feed.refresh()
        assert len(feed.snapshot() or []) == 5
        await http.aclose()

    asyncio.run(scenario())


def test_oversized_feed_is_refused(net: FakeNet, clock: Clock) -> None:
    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(net.handler))
        feed = BusyFeed(ICS_URL, BRT, 14, timedelta(hours=6), timedelta(minutes=10), http, clock)
        net.ics = "X" * (6 * 1024 * 1024)
        assert not await feed.refresh()
        assert feed.snapshot() is None
        await http.aclose()

    asyncio.run(scenario())
