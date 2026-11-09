"""Fixed-window counters in the database: rate limits and daily caps that survive restarts.

Every counter moves with a single conditional UPDATE (or an INSERT when the row is new), so two concurrent
requests can't both take the last unit, on SQLite and on Postgres alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import RateCounter, affected


class LimitReached(Exception):
    def __init__(self, reason: str, retry_at: datetime) -> None:
        super().__init__(reason)
        self.reason, self.retry_at = reason, retry_at


@dataclass(frozen=True)
class Window:
    bucket: str
    seconds: int
    limit: int

    def start(self, now: float) -> int:
        t = int(now)
        return t - t % self.seconds

    def retry_at(self, now: float) -> datetime:
        return datetime.fromtimestamp(self.start(now) + self.seconds, UTC)


def used(db: Session, w: Window, key: str, now: float) -> int:
    row = db.get(RateCounter, (w.bucket, key, w.start(now)))
    return int(row.count) if row else 0


def take(db: Session, w: Window, key: str, now: float, amount: int = 1) -> None:
    """Count ``amount`` units (committed); raise LimitReached if the window is already at its limit.

    With ``amount`` > 1 (token budgets) the last call may overshoot; the next one is refused."""
    start = w.start(now)
    for _ in range(3):
        res = db.execute(
            update(RateCounter)
            .where(
                RateCounter.bucket == w.bucket,
                RateCounter.key == key,
                RateCounter.window_start == start,
                RateCounter.count < w.limit,
            )
            .values(count=RateCounter.count + amount)
        )
        if affected(res):
            db.commit()
            return
        if db.get(RateCounter, (w.bucket, key, start)) is not None:
            db.rollback()
            raise LimitReached(w.bucket, w.retry_at(now))
        try:
            db.add(RateCounter(bucket=w.bucket, key=key, window_start=start, count=amount))
            db.commit()
            return
        except IntegrityError:
            db.rollback()
    raise LimitReached(w.bucket, w.retry_at(now))
