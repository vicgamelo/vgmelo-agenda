"""Shared fixtures. Every outbound call (Resend, the ICS feed, the LLM providers) goes through ``FakeNet``,
an httpx transport, so the app code runs unchanged down to the HTTP request. The clock is fixed."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from vgmelo_agenda.app import create_app
from vgmelo_agenda.config import Settings

SITE = "https://vgmelo.tech"
ORIGIN = {"Origin": SITE}
BRT = ZoneInfo("America/Sao_Paulo")
OWNER = "owner@example.com"
ICS_URL = "https://outlook.office365.com/owa/calendar/x/calendar.ics"
# Monday 2026-11-09, 09:00 in São Paulo.
T0 = datetime(2026, 11, 9, 9, 0, tzinfo=BRT)


def filled_ms() -> int:
    """started_at of a form opened 10 seconds ago."""
    return int(time.time() * 1000) - 10_000


def brt(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=BRT)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@dataclass
class FakeNet:
    emails: list[dict[str, Any]] = field(default_factory=list)
    ics: str | None = None
    ics_status: int = 200
    resend_status: int = 200
    llm: dict[str, list[Any]] = field(default_factory=dict)  # host -> queued responses (dict or int status)
    llm_requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.resend.com":
            if self.resend_status != 200:
                return httpx.Response(self.resend_status, json={"message": "nope"})
            self.emails.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "em_1"})
        if host == "outlook.office365.com":
            if self.ics is None or self.ics_status != 200:
                return httpx.Response(self.ics_status if self.ics is not None else 404)
            return httpx.Response(200, text=self.ics)
        if host in self.llm:
            self.llm_requests.append((host, json.loads(request.content)))
            queue = self.llm[host]
            item = queue.pop(0) if queue else 500
            if isinstance(item, int):
                return httpx.Response(item, json={"error": "x"})
            return httpx.Response(200, json=item)
        raise AssertionError(f"unexpected request to {request.url}")

    # ---- helpers ----
    def to(self, address: str) -> list[dict[str, Any]]:
        return [e for e in self.emails if e["to"] == [address]]

    def last_to(self, address: str) -> dict[str, Any]:
        mails = self.to(address)
        assert mails, f"no e-mail to {address}"
        return mails[-1]

    def token(self, address: str) -> str:
        m = re.search(r"#([A-Za-z0-9_\-]{20,})", self.last_to(address)["text"])
        assert m, "no manage link in the e-mail"
        return m.group(1)

    def owner_token(self) -> str:
        return self.token(OWNER)

    @staticmethod
    def ics_of(mail: dict[str, Any]) -> str:
        return base64.b64decode(mail["attachments"][0]["content"]).decode()


def make_settings(tmp_path: Any, **overrides: Any) -> Settings:
    url = os.environ.get("AGENDA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/agenda.db"
    base: dict[str, Any] = {
        "database_url": url,
        "secret_key": secrets.token_urlsafe(48),  # fresh per test, nothing secret-looking in the repo
        "site_url": SITE,
        "resend_api_key": "re_test",
        "owner_email": OWNER,
        "min_fill_seconds": 0,
        "meeting_url": "https://meet.example/victor",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@dataclass
class Env:
    client: TestClient
    net: FakeNet
    clock: Clock
    app: Any

    @property
    def ctx(self) -> Any:
        return self.app.api.state.ctx

    def book(self, when: datetime, email: str = "ana@example.com", **kw: Any) -> httpx.Response:
        body: dict[str, Any] = {
            "kind": "job",
            "start": when.isoformat(),
            "duration": 30,
            "name": "Ana Recruiter",
            "email": email,
            "company": "ACME",
            "topic": "Vaga de MLOps sênior",
            "lang": "pt",
            "tz": "America/Sao_Paulo",
            "started_at": filled_ms(),
        }
        body.update(kw)
        return self.client.post("/api/agenda/bookings", json=body, headers=ORIGIN)

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self.client.post(f"/api/agenda{path}", json=body, headers=ORIGIN)

    def slots(self, kind: str = "job", duration: int = 30) -> list[str]:
        r = self.client.get("/api/agenda/slots", params={"kind": kind, "duration": duration})
        assert r.status_code == 200, r.text
        return [s for d in r.json()["days"] for s in d["starts"]]

    def confirm_new(self, start: datetime, email: str = "ana@example.com", **kw: Any) -> str:
        r = self.book(start, email, **kw)
        assert r.status_code == 202, r.text
        token = self.net.token(email)
        assert self.post("/booking/confirm", {"token": token}).status_code == 200
        return token


EnvFactory = Callable[..., Env]


@pytest.fixture
def make_env(tmp_path: Any, net: FakeNet, clock: Clock) -> Iterator[EnvFactory]:
    opened: list[TestClient] = []
    engines: list[Any] = []

    def factory(**overrides: Any) -> Env:
        app = create_app(make_settings(tmp_path, **overrides), transport=httpx.MockTransport(net.handler), clock=clock)
        engines.append(app.api.state.engine)  # type: ignore[attr-defined]
        if os.environ.get("AGENDA_TEST_DATABASE_URL"):
            from vgmelo_agenda.db import Base

            Base.metadata.drop_all(engines[-1])
            Base.metadata.create_all(engines[-1])
        client = TestClient(app, base_url="https://vgmelo.tech", client=("203.0.113.7", 5000))
        client.__enter__()
        opened.append(client)
        return Env(client, net, clock, app)

    yield factory
    for c in opened:
        c.__exit__(None, None, None)
    for e in engines:
        e.dispose()


@pytest.fixture
def env(make_env: EnvFactory) -> Env:
    return make_env()
