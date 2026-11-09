from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetimes on both Postgres and SQLite (which drops the offset)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime")
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        # SQLite drops the offset; Postgres returns the session's timezone. Always hand back UTC.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    start_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime)
    duration: Mapped[int] = mapped_column(Integer)
    day: Mapped[str] = mapped_column(String(10))  # local date (owner's timezone) the per-day limit counts on
    # pending -> confirmed -> cancelled; pending -> expired | cancelled
    status: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(320), index=True)
    company: Mapped[str | None] = mapped_column(String(100))
    topic: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(2))
    guest_tz: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(10))  # web | mcp | agent
    guest_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    owner_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    hold_until: Mapped[datetime] = mapped_column(UTCDateTime)
    sequence: Mapped[int] = mapped_column(Integer, default=0)  # iCalendar SEQUENCE, bumped on reschedule
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_by: Mapped[str | None] = mapped_column(String(10))  # guest | owner

    __table_args__ = (Index("ix_bookings_start", "start_at"),)


class SlotCell(Base):
    """One row per 5-minute cell a live booking occupies (its time plus the buffer after it).

    The primary key is what makes two overlapping bookings impossible, whatever the isolation level:
    the second INSERT of the same cell fails."""

    __tablename__ = "slot_cells"

    cell: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # epoch minutes // 5
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)


class DayLoad(Base):
    """Live bookings per local day, raised with a conditional UPDATE so the daily limit holds under races."""

    __tablename__ = "day_load"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class RateCounter(Base):
    """Fixed-window counters (rate limits, daily caps). ``key`` is already hashed."""

    __tablename__ = "rate_counters"

    bucket: Mapped[str] = mapped_column(String(40), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0)


def _sqlite_immediate(engine: Engine) -> None:
    """pysqlite starts transactions lazily and in DEFERRED mode, so two writers can deadlock on the lock
    upgrade. BEGIN IMMEDIATE takes the write lock up front; the other writer waits (busy timeout)."""

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn: Any, _: Any) -> None:
        dbapi_conn.isolation_level = None
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=10000")

    @event.listens_for(engine, "begin")
    def _begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def make_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
    if url in {"sqlite://", "sqlite:///:memory:"}:
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        _sqlite_immediate(engine)
    Base.metadata.create_all(engine)
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Any]:
    return sessionmaker(engine, expire_on_commit=False)


def affected(result: Any) -> int:
    """Rows touched by an UPDATE/DELETE (``Result`` is typed without ``rowcount``)."""
    return int(result.rowcount or 0)
