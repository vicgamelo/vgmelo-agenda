"""Pure scheduling rules: which start times are open, given the settings, the clock and what's taken.

Nothing here touches the database, so every rule is tested with plain values.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .config import Settings


@dataclass(frozen=True)
class Kind:
    min_minutes: int
    max_minutes: int


# Confirmed by Victor: the guest picks the length within these bounds.
KINDS: dict[str, Kind] = {"job": Kind(15, 60), "consulting": Kind(30, 90)}


@dataclass(frozen=True)
class Interval:
    start: datetime  # aware, UTC
    end: datetime

    def overlaps(self, other: Interval, pad: timedelta = timedelta(0)) -> bool:
        return self.start < other.end + pad and other.start < self.end + pad


def durations(settings: Settings, kind: str) -> list[int]:
    k = KINDS[kind]
    step = settings.step_minutes
    first = -(-k.min_minutes // step) * step
    return list(range(first, k.max_minutes + 1, step))


def local_day(settings: Settings, when: datetime) -> str:
    return when.astimezone(settings.tz).date().isoformat()


def candidate_starts(settings: Settings, duration: int, now: datetime) -> list[datetime]:
    """Every start inside an opening window, on the step grid, between min notice and the horizon."""
    tz = settings.tz
    earliest = now + timedelta(hours=settings.min_notice_hours)
    latest = now + timedelta(days=settings.max_days_ahead)
    length = timedelta(minutes=duration)
    step = timedelta(minutes=settings.step_minutes)
    out: list[datetime] = []
    today = now.astimezone(tz).date()
    for offset in range(settings.max_days_ahead + 1):
        day = today + timedelta(days=offset)
        if day.weekday() not in settings.open_weekdays:
            continue
        for w_start, w_end in settings.day_windows:
            t = datetime.combine(day, w_start, tz)
            end_of_window = datetime.combine(day, w_end, tz)
            while t + length <= end_of_window:
                if earliest <= t <= latest:
                    out.append(t.astimezone(UTC))
                t += step
    return out


def free_starts(
    settings: Settings,
    duration: int,
    now: datetime,
    taken: Iterable[Interval],
    busy: Iterable[Interval],
    full_days: Iterable[str],
) -> list[datetime]:
    """Starts that keep ``buffer_minutes`` clear of every taken booking and busy calendar event, on days
    that haven't reached ``max_per_day``."""
    pad = timedelta(minutes=settings.buffer_minutes)
    blocked = sorted([*taken, *busy], key=lambda i: i.start)
    full = set(full_days)
    length = timedelta(minutes=duration)
    out = []
    for start in candidate_starts(settings, duration, now):
        if local_day(settings, start) in full:
            continue
        slot = Interval(start, start + length)
        if any(slot.overlaps(b, pad) for b in blocked):
            continue
        out.append(start)
    return out


def is_valid_duration(settings: Settings, kind: str, duration: int) -> bool:
    return kind in KINDS and duration in durations(settings, kind)


def group_by_day(settings: Settings, starts: Iterable[datetime], tz_name: str | None = None) -> dict[str, list[str]]:
    """{local date: ["HH:MM", ...]} in ``tz_name`` (default: the owner's timezone)."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name) if tz_name else settings.tz
    days: dict[str, list[str]] = {}
    for s in starts:
        local = s.astimezone(tz)
        days.setdefault(local.date().isoformat(), []).append(local.strftime("%H:%M"))
    return days


def window_dates(settings: Settings, now: datetime) -> tuple[date, date]:
    today = now.astimezone(settings.tz).date()
    return today, today + timedelta(days=settings.max_days_ahead)
