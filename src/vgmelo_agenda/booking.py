"""Booking lifecycle: create (pending, holds the slot) -> confirm by e-mail link -> cancel / reschedule.

Shared by the web API, the MCP server and the scheduling agent, so the rules and limits are the same
whichever door a request comes through.

Concurrency: a booking claims its 5-minute cells (``SlotCell`` primary key) and one unit of its day's
``DayLoad`` (conditional UPDATE) in the same transaction as the booking row. Two overlapping requests
can both pass the availability check, but only one commits; the other gets ``slot_taken``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import emails, rules
from .calendar_feed import BusyFeed
from .config import Settings
from .db import Booking, DayLoad, SlotCell, affected
from .limits import LimitReached, Window, take
from .mailer import Mailer
from .security import Keyring, new_id, new_token, sha256

logger = logging.getLogger(__name__)

LIVE = ("pending", "confirmed")
CELL_MINUTES = 5
Source = Literal["web", "mcp", "agent"]


class BookingError(Exception):
    def __init__(self, code: str, status: int = 409, retry_at: datetime | None = None) -> None:
        super().__init__(code)
        self.code, self.status, self.retry_at = code, status, retry_at


@dataclass(frozen=True)
class BookingRequest:
    kind: str
    start: datetime
    duration: int
    name: str
    email: str
    topic: str
    company: str | None = None
    lang: str = "pt"
    guest_tz: str | None = None


def _cells(start: datetime, end: datetime, buffer_minutes: int) -> range:
    a = int(start.timestamp()) // 60
    b = int((end + timedelta(minutes=buffer_minutes)).timestamp()) // 60
    return range(a // CELL_MINUTES, -(-b // CELL_MINUTES))


def _insert_day(db: Session, day: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        db.execute(pg_insert(DayLoad).values(day=day, count=0).on_conflict_do_nothing())
    else:
        from sqlalchemy.dialects.sqlite import insert as lite_insert

        db.execute(lite_insert(DayLoad).values(day=day, count=0).on_conflict_do_nothing())


class Agenda:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Any],
        mailer: Mailer,
        feed: BusyFeed,
        keyring: Keyring,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings, self.sessions, self.mailer, self.feed = settings, sessions, mailer, feed
        self.keyring, self.clock = keyring, clock

    # ---------- reading ----------

    def _expire_holds(self, db: Session, now: datetime) -> None:
        rows = db.execute(
            select(Booking.id, Booking.day).where(Booking.status == "pending", Booking.hold_until <= now)
        ).all()
        for booking_id, day in rows:
            res = db.execute(
                update(Booking).where(Booking.id == booking_id, Booking.status == "pending").values(status="expired")
            )
            if affected(res):
                self._release(db, booking_id, day)
        db.commit()

    def _busy(self) -> list[rules.Interval]:
        busy = self.feed.snapshot()
        if busy is None:
            raise BookingError("calendar_unavailable", 503)
        return busy

    def _free(self, db: Session, kind: str, duration: int, now: datetime, exclude: str | None = None) -> list[datetime]:
        if not rules.is_valid_duration(self.settings, kind, duration):
            raise BookingError("invalid_duration", 422)
        busy = self._busy()
        q = select(Booking.id, Booking.start_at, Booking.end_at, Booking.day).where(
            Booking.status.in_(LIVE), Booking.end_at > now - timedelta(minutes=self.settings.buffer_minutes)
        )
        taken: list[rules.Interval] = []
        per_day: dict[str, int] = {}
        for bid, s, e, day in db.execute(q).all():
            if bid == exclude:
                continue
            taken.append(rules.Interval(s, e))
            per_day[day] = per_day.get(day, 0) + 1
        full = [d for d, n in per_day.items() if n >= self.settings.max_per_day]
        return rules.free_starts(self.settings, duration, now, taken, busy, full)

    def free_starts(self, kind: str, duration: int) -> list[datetime]:
        now = self.clock()
        with self.sessions() as db:
            self._expire_holds(db, now)
            return self._free(db, kind, duration, now)

    def calendar_ok(self) -> bool:
        return self.feed.snapshot() is not None

    # ---------- claiming ----------

    def _claim(self, db: Session, b: Booking) -> None:
        _insert_day(db, b.day)
        res = db.execute(
            update(DayLoad)
            .where(DayLoad.day == b.day, DayLoad.count < self.settings.max_per_day)
            .values(count=DayLoad.count + 1)
        )
        if not affected(res):
            raise BookingError("day_full")
        db.add_all(
            SlotCell(cell=c, booking_id=b.id) for c in _cells(b.start_at, b.end_at, self.settings.buffer_minutes)
        )
        db.flush()

    def _release(self, db: Session, booking_id: str, day: str) -> None:
        db.execute(delete(SlotCell).where(SlotCell.booking_id == booking_id))
        db.execute(update(DayLoad).where(DayLoad.day == day, DayLoad.count > 0).values(count=DayLoad.count - 1))

    def _check_start(
        self, db: Session, kind: str, start: datetime, duration: int, now: datetime, exclude: str | None
    ) -> datetime:
        if start.tzinfo is None:
            raise BookingError("naive_datetime", 422)
        start = start.astimezone(UTC)
        if start not in self._free(db, kind, duration, now, exclude):
            raise BookingError("slot_unavailable")
        return start

    # ---------- create ----------

    def create(self, req: BookingRequest, ip: str, source: Source) -> Booking:
        s, now = self.settings, self.clock()
        if not self.mailer.enabled and not s.dev_log_links:
            raise BookingError("email_unavailable", 503)
        email = req.email.strip().lower()
        with self.sessions() as db:
            try:
                take(db, Window("book_ip", 3600, s.booking_max_per_ip_hour), self.keyring.mac(ip), now.timestamp())
            except LimitReached as exc:
                raise BookingError("rate_limited", 429, exc.retry_at) from exc
            self._expire_holds(db, now)
            if req.kind not in rules.KINDS:
                raise BookingError("invalid_kind", 422)
            start = self._check_start(db, req.kind, req.start, req.duration, now, None)
            mine = db.execute(
                select(Booking.status, func.count())
                .where(Booking.email == email, Booking.status.in_(LIVE), Booking.start_at > now)
                .group_by(Booking.status)
            ).all()
            counts = {st: n for st, n in mine}
            if counts.get("pending"):
                raise BookingError("pending_exists")
            if counts.get("confirmed", 0) >= s.max_future_per_email:
                raise BookingError("too_many_bookings")
            pending_all = db.scalar(select(func.count()).select_from(Booking).where(Booking.status == "pending")) or 0
            if pending_all >= s.max_pending_global:
                raise BookingError("agenda_busy", 503)

            token = new_token()
            b = Booking(
                id=new_id(),
                kind=req.kind,
                start_at=start,
                end_at=start + timedelta(minutes=req.duration),
                duration=req.duration,
                day=rules.local_day(s, start),
                status="pending",
                name=req.name.strip(),
                email=email,
                company=(req.company or "").strip() or None,
                topic=req.topic.strip(),
                lang=req.lang if req.lang in ("pt", "en") else "pt",
                guest_tz=req.guest_tz,
                source=source,
                guest_token_hash=sha256(token),
                hold_until=now + timedelta(minutes=s.hold_minutes),
                created_at=now,
            )
            try:
                db.add(b)
                db.flush()
                self._claim(db, b)
                db.commit()
            except IntegrityError:
                db.rollback()
                raise BookingError("slot_taken") from None
            except BookingError:
                db.rollback()
                raise

            # The confirmation e-mail is the abuse vector (any address can be typed), so it has its own caps.
            try:
                take(db, Window("mail_day", 86400, s.max_emails_day), "all", now.timestamp())
                take(db, Window("mail_to", 86400, s.emails_per_address_day), self.keyring.mac(email), now.timestamp())
            except LimitReached as exc:
                self._drop(db, b)
                raise BookingError("email_limit", 429, exc.retry_at) from exc
            sent = self.mailer.send(emails.pending(s, b, token))
            if not sent and s.dev_log_links:
                logger.warning("DEV link for %s: %s", b.id, emails.manage_url(s, b.lang, token))
                sent = True
            if not sent:
                self._drop(db, b)
                raise BookingError("email_failed", 502)
            logger.info("booking %s pending via %s", b.id, source)
            return b

    def _drop(self, db: Session, b: Booking) -> None:
        res = db.execute(
            update(Booking).where(Booking.id == b.id, Booking.status == "pending").values(status="expired")
        )
        if affected(res):
            self._release(db, b.id, b.day)
        db.commit()

    # ---------- token actions ----------

    def _find(self, db: Session, token: str) -> tuple[Booking, str]:
        if not token or len(token) > 100:
            raise BookingError("not_found", 404)
        h = sha256(token)
        b = db.scalar(select(Booking).where(Booking.guest_token_hash == h))
        if b is not None:
            return b, "guest"
        b = db.scalar(select(Booking).where(Booking.owner_token_hash == h))
        if b is not None:
            return b, "owner"
        raise BookingError("not_found", 404)

    def _can_change(self, b: Booking, role: str, now: datetime) -> bool:
        if b.status not in LIVE or b.start_at <= now:
            return False
        if role == "owner" or b.status == "pending":
            return True
        return b.start_at - now >= timedelta(hours=self.settings.change_cutoff_hours)

    def view(self, token: str) -> dict[str, Any]:
        now = self.clock()
        with self.sessions() as db:
            self._expire_holds(db, now)
            b, role = self._find(db, token)
            out: dict[str, Any] = {
                "role": role,
                "status": b.status,
                "kind": b.kind,
                "duration": b.duration,
                "start": b.start_at.isoformat(),
                "end": b.end_at.isoformat(),
                "lang": b.lang,
                "name": b.name,
                "hold_until": b.hold_until.isoformat() if b.status == "pending" else None,
                "can_confirm": role == "guest" and b.status == "pending",
                "can_cancel": self._can_change(b, role, now),
                "can_reschedule": role == "guest" and b.status == "confirmed" and self._can_change(b, role, now),
                "durations": rules.durations(self.settings, b.kind),
                "meeting_url": self.settings.meeting_url if b.status == "confirmed" else "",
            }
            if role == "owner":
                out |= {"email": b.email, "company": b.company, "topic": b.topic, "source": b.source}
            return out

    def confirm(self, token: str) -> str:
        now = self.clock()
        with self.sessions() as db:
            b, role = self._find(db, token)
            if role != "guest":
                raise BookingError("forbidden", 403)
            if b.status == "confirmed":
                return "confirmed"
            if b.status == "pending" and b.hold_until <= now:
                self._expire_holds(db, now)
                raise BookingError("expired", 410)
            if b.status == "expired":
                raise BookingError("expired", 410)
            if b.status != "pending":
                raise BookingError(b.status, 409)
            owner_token = new_token()
            res = db.execute(
                update(Booking)
                .where(Booking.id == b.id, Booking.status == "pending", Booking.hold_until > now)
                .values(status="confirmed", confirmed_at=now, owner_token_hash=sha256(owner_token))
            )
            if not affected(res):
                db.rollback()
                raise BookingError("expired", 410)
            db.commit()
            db.refresh(b)
        self.mailer.send(emails.confirmed_guest(self.settings, b, token))
        if not self.mailer.send(emails.owner_notice(self.settings, b, owner_token)) and self.settings.dev_log_links:
            logger.warning("DEV owner link for %s: %s", b.id, emails.manage_url(self.settings, "pt", owner_token))
        logger.info("booking %s confirmed", b.id)
        return "confirmed"

    def cancel(self, token: str) -> str:
        now = self.clock()
        with self.sessions() as db:
            b, role = self._find(db, token)
            if b.status not in LIVE:
                raise BookingError(b.status, 409)
            if not self._can_change(b, role, now):
                raise BookingError("too_late", 409)
            was = b.status
            res = db.execute(
                update(Booking)
                .where(Booking.id == b.id, Booking.status == was)
                .values(status="cancelled", cancelled_at=now, cancelled_by=role)
            )
            if not affected(res):
                db.rollback()
                raise BookingError("conflict", 409)
            self._release(db, b.id, b.day)
            db.commit()
            db.refresh(b)
        if was == "confirmed":
            self.mailer.send(emails.cancelled_guest(self.settings, b))
            self.mailer.send(emails.cancelled_owner(self.settings, b))
        logger.info("booking %s cancelled by %s", b.id, role)
        return "cancelled"

    def reschedule(self, token: str, start: datetime, duration: int) -> str:
        s, now = self.settings, self.clock()
        with self.sessions() as db:
            self._expire_holds(db, now)
            b, role = self._find(db, token)
            if role != "guest" or b.status != "confirmed":
                raise BookingError("forbidden" if role != "guest" else b.status, 403 if role != "guest" else 409)
            if not self._can_change(b, role, now):
                raise BookingError("too_late", 409)
            new_start = self._check_start(db, b.kind, start, duration, now, exclude=b.id)
            owner_token = new_token()
            old_day = b.day
            try:
                self._release(db, b.id, old_day)
                b.start_at = new_start
                b.end_at = new_start + timedelta(minutes=duration)
                b.duration = duration
                b.day = rules.local_day(s, new_start)
                b.sequence += 1
                b.owner_token_hash = sha256(owner_token)
                db.flush()
                self._claim(db, b)
                db.commit()
            except IntegrityError:
                db.rollback()
                raise BookingError("slot_taken") from None
            except BookingError:
                db.rollback()
                raise
        self.mailer.send(emails.confirmed_guest(s, b, token, rescheduled=True))
        if not self.mailer.send(emails.owner_notice(s, b, owner_token, rescheduled=True)) and s.dev_log_links:
            logger.warning("DEV owner link for %s: %s", b.id, emails.manage_url(s, "pt", owner_token))
        logger.info("booking %s rescheduled", b.id)
        return "confirmed"
