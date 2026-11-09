"""Booking flow through the HTTP API: create, confirm, cancel, reschedule, limits, races, timezones."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from tests.conftest import BRT, ORIGIN, OWNER, Env, EnvFactory, FakeNet, brt, filled_ms
from vgmelo_agenda.booking import BookingRequest
from vgmelo_agenda.db import DayLoad, SlotCell

TUE_8 = brt(10, 8, 0)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_happy_path_pending_confirm_and_emails(env: Env) -> None:
    assert iso_z(TUE_8) in env.slots()
    r = env.book(TUE_8)
    assert r.status_code == 202 and r.json() == {"status": "pending", "hold_minutes": 30}
    # Held while pending: gone from the list for everyone.
    assert iso_z(TUE_8) not in env.slots()

    pending = env.net.last_to("ana@example.com")
    assert "Confirme" in pending["subject"]
    # Nothing the guest typed goes into the guest's e-mail (no free spam channel).
    for typed in ("Ana Recruiter", "ACME", "MLOps"):
        assert typed not in pending["text"]
    assert "https://vgmelo.tech/agenda/reserva#" in pending["text"]
    assert env.net.to(OWNER) == []  # Victor hears about it only once it's confirmed

    token = env.net.token("ana@example.com")
    view = env.post("/booking", {"token": token}).json()
    assert view["status"] == "pending" and view["can_confirm"] and view["role"] == "guest"
    assert "email" not in view and "topic" not in view

    assert env.post("/booking/confirm", {"token": token}).json() == {"status": "confirmed"}
    guest = env.net.last_to("ana@example.com")
    assert "confirmada" in guest["subject"] and "https://meet.example/victor" in guest["text"]
    ics = FakeNet.ics_of(guest)
    assert "DTSTART:20261110T110000Z" in ics and "METHOD:PUBLISH" in ics
    owner = env.net.last_to(OWNER)
    assert owner["reply_to"] == "ana@example.com"
    assert "Vaga de MLOps sênior" in owner["text"] and "ACME" in owner["text"]
    assert "DTEND:20261110T113000Z" in FakeNet.ics_of(owner)

    # Confirming twice is harmless and sends nothing new.
    sent = len(env.net.emails)
    assert env.post("/booking/confirm", {"token": token}).json() == {"status": "confirmed"}
    assert len(env.net.emails) == sent


def test_owner_email_never_reaches_the_browser(env: Env) -> None:
    token = env.confirm_new(TUE_8)
    for body in (
        env.client.get("/api/agenda/config").text,
        env.client.get("/api/agenda/slots", params={"kind": "job", "duration": 30}).text,
        env.post("/booking", {"token": token}).text,
    ):
        assert OWNER not in body


def test_slot_held_by_someone_else_is_refused(env: Env) -> None:
    assert env.book(TUE_8).status_code == 202
    r = env.book(TUE_8, email="bob@example.com")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "slot_unavailable"
    # Overlapping (08:15) and right after it with no gap (08:30) are refused; 08:45 leaves the 15-min buffer.
    assert env.book(brt(10, 8, 15), email="bob@example.com").status_code == 409
    assert env.book(brt(10, 8, 30), email="bob@example.com").status_code == 409
    assert env.book(brt(10, 8, 45), email="bob@example.com").status_code == 202


def test_off_grid_outside_window_and_naive_times_are_refused(make_env: EnvFactory) -> None:
    env = make_env(booking_max_per_ip_hour=50)  # failed attempts count towards the IP limit too
    assert env.book(brt(10, 8, 10)).json()["detail"]["code"] == "slot_unavailable"
    assert env.book(brt(10, 10, 0)).json()["detail"]["code"] == "slot_unavailable"  # window ends at 10:00
    assert env.book(brt(15, 8, 0)).json()["detail"]["code"] == "slot_unavailable"  # Sunday
    assert env.book(brt(9, 18, 0)).json()["detail"]["code"] == "slot_unavailable"  # less than 12h ahead
    assert env.book(brt(24, 8, 0)).json()["detail"]["code"] == "slot_unavailable"  # beyond 14 days
    r = env.book(TUE_8, start="2026-11-10T08:00:00")
    assert r.status_code == 422 and r.json()["detail"]["fields"] == ["start"]
    r = env.book(TUE_8, duration=75)  # job is 15-60
    assert r.json()["detail"]["code"] == "invalid_duration"
    assert env.book(TUE_8, kind="consulting", duration=90).status_code == 202


def test_same_instant_in_another_timezone_books_the_same_slot(env: Env) -> None:
    lisbon = TUE_8.astimezone(ZoneInfo("Europe/Lisbon"))
    assert lisbon.hour == 11
    r = env.book(lisbon, tz="Europe/Lisbon")
    assert r.status_code == 202
    text = env.net.last_to("ana@example.com")["text"]
    assert "ter, 10/11/2026 às 08:00 (America/Sao_Paulo)" in text
    assert "ter, 10/11/2026 às 11:00 (no seu fuso, Europe/Lisbon)" in text
    # A bogus tz name is ignored, not stored.
    env.post("/booking/cancel", {"token": env.net.token("ana@example.com")})
    assert env.book(TUE_8, tz="Mars/Olympus", email="z@example.com").status_code == 202
    assert "no seu fuso" not in env.net.last_to("z@example.com")["text"]


def test_english_booking_gets_english_mail_and_link(env: Env) -> None:
    env.book(TUE_8, lang="en")
    mail = env.net.last_to("ana@example.com")
    assert mail["subject"].startswith("Confirm your call")
    assert "https://vgmelo.tech/en/schedule/booking#" in mail["text"]
    assert "Tue, Nov 10, 2026 at 08:00" in mail["text"]


def test_hold_expires_after_30_minutes(env: Env) -> None:
    env.book(TUE_8)
    token = env.net.token("ana@example.com")
    env.clock.advance(minutes=30)
    assert iso_z(TUE_8) in env.slots()
    r = env.post("/booking/confirm", {"token": token})
    assert r.status_code == 410 and r.json()["detail"]["code"] == "expired"
    # The slot and the day are free again: someone else gets it.
    assert env.book(TUE_8, email="bob@example.com").status_code == 202


def test_two_per_day(env: Env) -> None:
    env.confirm_new(TUE_8, "a@example.com")
    env.confirm_new(brt(10, 12, 0), "b@example.com")
    starts = env.slots()
    assert not [s for s in starts if s.startswith("2026-11-10")]
    r = env.book(brt(10, 18, 0), email="c@example.com")
    assert r.status_code == 409
    # Next day is untouched.
    assert env.book(brt(11, 18, 0), email="c@example.com").status_code == 202


def test_day_limit_counts_the_owners_day_not_utc(env: Env) -> None:
    # 19:00 BRT is 22:00 UTC (same BRT day as 08:00), so it counts towards Tuesday's two.
    env.confirm_new(TUE_8, "a@example.com")
    env.confirm_new(brt(10, 19, 0), "b@example.com")
    assert env.book(brt(10, 12, 0), email="c@example.com").status_code == 409


def test_one_pending_per_email_and_two_future_per_email(env: Env) -> None:
    assert env.book(TUE_8).status_code == 202
    r = env.book(brt(11, 8, 0))
    assert r.json()["detail"]["code"] == "pending_exists"
    env.post("/booking/confirm", {"token": env.net.token("ana@example.com")})
    env.confirm_new(brt(11, 8, 0))
    r = env.book(brt(12, 8, 0), email="ANA@example.com")  # case doesn't matter
    assert r.json()["detail"]["code"] == "too_many_bookings"


def test_global_pending_cap(make_env: EnvFactory) -> None:
    env = make_env(max_pending_global=2, booking_max_per_ip_hour=50)
    assert env.book(TUE_8, "a@example.com").status_code == 202
    assert env.book(brt(11, 8, 0), "b@example.com").status_code == 202
    r = env.book(brt(12, 8, 0), "c@example.com")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "agenda_busy"


def test_rate_limit_per_ip(make_env: EnvFactory) -> None:
    env = make_env(booking_max_per_ip_hour=2)
    assert env.book(TUE_8, "a@example.com").status_code == 202
    assert env.book(brt(11, 8, 0), "b@example.com").status_code == 202
    r = env.book(brt(12, 8, 0), "c@example.com")
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_daily_email_cap_releases_the_slot(make_env: EnvFactory) -> None:
    env = make_env(max_emails_day=1, booking_max_per_ip_hour=50)
    assert env.book(TUE_8, "a@example.com").status_code == 202
    r = env.book(brt(11, 8, 0), "b@example.com")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "email_limit"
    assert iso_z(brt(11, 8, 0)) in env.slots()


def test_failed_email_releases_the_slot(env: Env) -> None:
    env.net.resend_status = 500
    r = env.book(TUE_8)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "email_failed"
    assert iso_z(TUE_8) in env.slots()


def test_no_resend_key_means_no_booking(make_env: EnvFactory) -> None:
    env = make_env(resend_api_key="")
    assert env.book(TUE_8).json()["detail"]["code"] == "email_unavailable"


def test_honeypot_and_too_fast_get_a_fake_success(make_env: EnvFactory) -> None:
    env = make_env(min_fill_seconds=3)
    import time

    now_ms = int(time.time() * 1000)
    assert env.book(TUE_8, website="http://spam", started_at=0).status_code == 202
    assert env.book(TUE_8, started_at=now_ms).status_code == 202  # filled in under 3s
    assert env.net.emails == []
    assert iso_z(TUE_8) in env.slots()


def test_post_without_our_origin_is_refused(env: Env) -> None:
    r = env.client.post("/api/agenda/bookings", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert env.client.post("/api/agenda/bookings", json={}).status_code == 403


def test_unknown_fields_are_refused(env: Env) -> None:
    r = env.book(TUE_8, status="confirmed")
    assert r.status_code == 422 and "status" in r.json()["detail"]["fields"]


def test_guest_cancels_and_both_are_told(env: Env) -> None:
    token = env.confirm_new(TUE_8)
    env.net.emails.clear()
    assert env.post("/booking/cancel", {"token": token}).json() == {"status": "cancelled"}
    assert iso_z(TUE_8) in env.slots()
    assert "cancelada" in env.net.last_to("ana@example.com")["subject"]
    assert "METHOD:CANCEL" in FakeNet.ics_of(env.net.last_to(OWNER))
    assert "Cancelada por Ana Recruiter" in env.net.last_to(OWNER)["text"]
    r = env.post("/booking/cancel", {"token": token})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cancelled"
    # And the day count went back down: two more fit on Tuesday.
    env.confirm_new(brt(10, 12, 0), "b@example.com")
    env.confirm_new(brt(10, 18, 0), "c@example.com")


def test_cancelling_a_pending_request_sends_nothing(env: Env) -> None:
    env.book(TUE_8)
    token = env.net.token("ana@example.com")
    sent = len(env.net.emails)
    assert env.post("/booking/cancel", {"token": token}).status_code == 200
    assert len(env.net.emails) == sent
    assert iso_z(TUE_8) in env.slots()


def test_guest_cannot_cancel_within_2h_but_owner_can(env: Env) -> None:
    token = env.confirm_new(TUE_8)
    owner_token = env.net.owner_token()
    env.clock.now = brt(10, 6, 1).astimezone(UTC)  # 1h59 before
    view = env.post("/booking", {"token": token}).json()
    assert not view["can_cancel"] and not view["can_reschedule"]
    r = env.post("/booking/cancel", {"token": token})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "too_late"

    owner_view = env.post("/booking", {"token": owner_token}).json()
    assert owner_view["role"] == "owner" and owner_view["can_cancel"]
    assert owner_view["email"] == "ana@example.com" and owner_view["topic"] == "Vaga de MLOps sênior"
    assert env.post("/booking/cancel", {"token": owner_token}).json() == {"status": "cancelled"}
    assert "precisou cancelar" in env.net.last_to("ana@example.com")["text"]
    assert "Cancelada por você" in env.net.last_to(OWNER)["text"]


def test_guest_can_cancel_at_exactly_2h(env: Env) -> None:
    token = env.confirm_new(TUE_8)
    env.clock.now = brt(10, 6, 0).astimezone(UTC)
    assert env.post("/booking/cancel", {"token": token}).status_code == 200


def test_owner_cannot_confirm_or_reschedule(env: Env) -> None:
    env.confirm_new(TUE_8)
    owner_token = env.net.owner_token()
    assert env.post("/booking/confirm", {"token": owner_token}).status_code == 403
    r = env.post("/booking/reschedule", {"token": owner_token, "start": brt(11, 8).isoformat(), "duration": 30})
    assert r.status_code == 403


def test_reschedule_moves_the_booking(env: Env) -> None:
    token = env.confirm_new(TUE_8)
    env.net.emails.clear()
    r = env.post("/booking/reschedule", {"token": token, "start": brt(11, 12, 0).isoformat(), "duration": 45})
    assert r.json() == {"status": "confirmed"}
    starts = env.slots()
    assert iso_z(TUE_8) in starts and iso_z(brt(11, 12, 0)) not in starts
    view = env.post("/booking", {"token": token}).json()
    assert view["start"] == brt(11, 12, 0).astimezone(UTC).isoformat() and view["duration"] == 45
    guest_ics = FakeNet.ics_of(env.net.last_to("ana@example.com"))
    assert "SEQUENCE:1" in guest_ics and "DTSTART:20261111T150000Z" in guest_ics
    assert "Remarcada" in env.net.last_to(OWNER)["text"]
    # The old owner link stopped working; the new one works.
    new_owner = env.net.owner_token()
    assert env.post("/booking", {"token": new_owner}).json()["role"] == "owner"


def test_reschedule_can_overlap_its_own_old_time(env: Env) -> None:
    token = env.confirm_new(TUE_8)
    r = env.post("/booking/reschedule", {"token": token, "start": brt(10, 8, 15).isoformat(), "duration": 30})
    assert r.status_code == 200


def test_failed_reschedule_keeps_the_old_booking(env: Env) -> None:
    token = env.confirm_new(TUE_8)
    env.confirm_new(brt(11, 8, 0), "b@example.com")
    r = env.post("/booking/reschedule", {"token": token, "start": brt(11, 8, 0).isoformat(), "duration": 30})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "slot_unavailable"
    view = env.post("/booking", {"token": token}).json()
    assert view["status"] == "confirmed" and view["start"] == TUE_8.astimezone(UTC).isoformat()
    assert iso_z(TUE_8) not in env.slots()


def test_reschedule_into_a_full_day_is_refused_but_within_the_same_day_is_fine(env: Env) -> None:
    token = env.confirm_new(TUE_8, "a@example.com")
    env.confirm_new(brt(11, 8, 0), "b@example.com")
    env.confirm_new(brt(11, 12, 0), "c@example.com")
    r = env.post("/booking/reschedule", {"token": token, "start": brt(11, 18, 0).isoformat(), "duration": 30})
    assert r.status_code == 409
    r = env.post("/booking/reschedule", {"token": token, "start": brt(10, 18, 0).isoformat(), "duration": 30})
    assert r.status_code == 200


def test_unknown_token(env: Env) -> None:
    r = env.post("/booking", {"token": "x" * 43})
    assert r.status_code == 404


def test_manage_endpoints_are_rate_limited(make_env: EnvFactory) -> None:
    env = make_env(manage_max_per_ip_hour=3)
    codes = [env.post("/booking", {"token": "x" * 43}).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]


def test_forwarded_for_only_from_trusted_proxy(make_env: EnvFactory) -> None:
    env = make_env(booking_max_per_ip_hour=1, trusted_proxies="203.0.113.0/24")
    hdr = {**ORIGIN, "X-Forwarded-For": "198.51.100.1"}
    body: dict[str, Any] = {
        "kind": "job", "duration": 30, "name": "A", "email": "a@example.com", "topic": "abc", "started_at": filled_ms(),
    }  # fmt: skip
    assert (
        env.client.post("/api/agenda/bookings", json={**body, "start": TUE_8.isoformat()}, headers=hdr).status_code
        == 202
    )
    hdr2 = {**ORIGIN, "X-Forwarded-For": "198.51.100.2"}
    body2 = {**body, "email": "b@example.com", "start": brt(11, 8).isoformat()}
    assert env.client.post("/api/agenda/bookings", json=body2, headers=hdr2).status_code == 202
    body3 = {**body, "email": "c@example.com", "start": brt(12, 8).isoformat()}
    assert env.client.post("/api/agenda/bookings", json=body3, headers=hdr2).status_code == 429


# ---------- races (real threads, real database) ----------


def _race(env: Env, requests: list[BookingRequest]) -> list[str]:
    agenda = env.ctx.agenda
    results: list[str] = []
    barrier = threading.Barrier(len(requests))

    def run(i: int, req: BookingRequest) -> None:
        barrier.wait()
        try:
            agenda.create(req, f"198.51.100.{i}", "web")
            results.append("ok")
        except Exception as exc:
            results.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=run, args=(i, r)) for i, r in enumerate(requests)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(results)


def _req(start: datetime, email: str) -> BookingRequest:
    return BookingRequest(kind="job", start=start, duration=30, name="X", email=email, topic="race")


def test_race_same_slot_only_one_wins(env: Env) -> None:
    results = _race(env, [_req(TUE_8, f"user{i}@example.com") for i in range(6)])
    assert results.count("ok") == 1, results
    assert set(results) <= {"ok", "slot_taken", "slot_unavailable"}
    with env.ctx.agenda.sessions() as db:
        assert db.query(DayLoad).filter_by(day="2026-11-10").one().count == 1


def test_race_overlapping_slots_only_one_wins(env: Env) -> None:
    starts = [brt(10, 8, 0), brt(10, 8, 15), brt(10, 8, 30), brt(10, 8, 45)]
    results = _race(env, [_req(s, f"u{i}@example.com") for i, s in enumerate(starts)])
    with env.ctx.agenda.sessions() as db:
        cells = {c.booking_id for c in db.query(SlotCell).all()}
    # 08:00 and 08:45 don't overlap (08:30 + 15 buffer = 08:45), so at most those two together.
    assert 1 <= results.count("ok") <= 2 and len(cells) == results.count("ok"), results


def test_race_day_limit_holds(env: Env) -> None:
    starts = [brt(10, 8, 0), brt(10, 9, 0), brt(10, 12, 0), brt(10, 13, 0), brt(10, 18, 0), brt(10, 19, 0)]
    results = _race(env, [_req(s, f"day{i}@example.com") for i, s in enumerate(starts)])
    assert results.count("ok") == 2, results
    with env.ctx.agenda.sessions() as db:
        assert db.query(DayLoad).filter_by(day="2026-11-10").one().count == 2


def test_view_never_shows_other_bookings(env: Env) -> None:
    env.confirm_new(TUE_8, "a@example.com")
    env.confirm_new(brt(11, 8, 0), "b@example.com")
    view = env.post("/booking", {"token": env.net.token("b@example.com")}).json()
    assert "a@example.com" not in str(view)


def test_times_are_stored_in_utc(env: Env) -> None:
    env.book(TUE_8)
    view = env.post("/booking", {"token": env.net.token("ana@example.com")}).json()
    assert view["start"] == "2026-11-10T11:00:00+00:00"
    assert datetime.fromisoformat(view["start"]).astimezone(BRT).hour == 8


def test_booking_fields_match_the_site() -> None:
    # Same list as BOOKING_FIELDS / CHAT_FIELDS in vgmelo.tech/src/lib/agenda.ts (extra="forbid" on both sides).
    from vgmelo_agenda.api import BookingIn, ChatIn

    site = {"kind", "start", "duration", "name", "email", "company", "topic", "lang", "tz", "website", "started_at"}
    assert set(BookingIn.model_fields) == site
    assert set(ChatIn.model_fields) == {"conversation", "message", "lang", "tz"}


def test_without_owner_email_the_guest_flow_still_works(make_env: EnvFactory) -> None:
    env = make_env(owner_email="")
    token = env.confirm_new(TUE_8)
    assert env.post("/booking", {"token": token}).json()["status"] == "confirmed"
    assert all(e["to"] == ["ana@example.com"] for e in env.net.emails)
