"""The .ics is a real invite (METHOD:REQUEST / CANCEL with an ATTENDEE), so Outlook and Gmail put it on the
calendar by themselves. With METHOD:PUBLISH and no attendee, Outlook showed it as a plain attachment."""

from __future__ import annotations

from typing import Any

import icalendar

from tests.conftest import OWNER, Env, FakeNet, brt

GUEST = "ana@example.com"
SENDER = "mailto:no-reply@vgmelo.tech"


def event(mail: dict[str, Any]) -> tuple[icalendar.Calendar, icalendar.Event]:
    cal = icalendar.Calendar.from_ical(FakeNet.ics_of(mail))
    (ev,) = cal.walk("VEVENT")
    return cal, ev


def check(mail: dict[str, Any], method: str, attendee: str) -> icalendar.Event:
    assert mail["attachments"][0]["content_type"] == f"text/calendar; charset=utf-8; method={method}"
    cal, ev = event(mail)
    assert cal["METHOD"] == method
    assert str(ev["ORGANIZER"]) == SENDER
    att = ev["ATTENDEE"]
    assert not isinstance(att, list), "one attendee per invite"
    assert str(att) == f"mailto:{attendee}"
    assert att.params["PARTSTAT"] == "NEEDS-ACTION" and att.params["RSVP"] == "FALSE"
    return ev


def test_confirmation_sends_a_request_to_each_side(env: Env) -> None:
    env.confirm_new(brt(10, 8))
    guest = check(env.net.last_to(GUEST), "REQUEST", GUEST)
    owner = check(env.net.last_to(OWNER), "REQUEST", OWNER)
    assert guest["UID"] == owner["UID"] and guest["SEQUENCE"] == owner["SEQUENCE"] == 0
    # Still nothing the guest typed in the guest's copy (no CN with their name).
    assert "Ana Recruiter" not in FakeNet.ics_of(env.net.last_to(GUEST))


def test_reschedule_updates_the_same_event(env: Env) -> None:
    token = env.confirm_new(brt(10, 8))
    uid = event(env.net.last_to(GUEST))[1]["UID"]
    env.net.emails.clear()
    r = env.post("/booking/reschedule", {"token": token, "start": brt(11, 12).isoformat(), "duration": 45})
    assert r.status_code == 200
    for mail, who in ((env.net.last_to(GUEST), GUEST), (env.net.last_to(OWNER), OWNER)):
        ev = check(mail, "REQUEST", who)
        assert ev["UID"] == uid and ev["SEQUENCE"] == 1


def test_cancel_removes_the_same_event(env: Env) -> None:
    token = env.confirm_new(brt(10, 8))
    uid = event(env.net.last_to(GUEST))[1]["UID"]
    env.net.emails.clear()
    assert env.post("/booking/cancel", {"token": token}).status_code == 200
    for mail, who in ((env.net.last_to(GUEST), GUEST), (env.net.last_to(OWNER), OWNER)):
        ev = check(mail, "CANCEL", who)
        assert ev["UID"] == uid and ev["SEQUENCE"] == 1 and ev["STATUS"] == "CANCELLED"
