"""E-mail texts (PT/EN) and the .ics invite.

What the guest receives never carries text the guest typed (name, company, topic): otherwise anyone could
use the booking form to send their own words to a third party's inbox from vgmelo.tech.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parseaddr
from zoneinfo import ZoneInfo

import icalendar

from .config import Settings
from .db import Booking
from .mailer import Email

KIND_LABEL = {
    "pt": {"job": "vaga / recrutamento", "consulting": "consultoria"},
    "en": {"job": "job / recruiting", "consulting": "consulting"},
}
_WEEKDAYS = {
    "pt": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def manage_url(settings: Settings, lang: str, token: str) -> str:
    path = "/agenda/reserva" if lang == "pt" else "/en/schedule/booking"
    # Fragment, not query: it never reaches server logs or the Referer header.
    return f"{settings.site_url}{path}#{token}"


def book_url(settings: Settings, lang: str) -> str:
    return f"{settings.site_url}{'/agenda' if lang == 'pt' else '/en/schedule'}"


def fmt_when(when: datetime, tz: ZoneInfo, lang: str) -> str:
    t = when.astimezone(tz)
    wd = _WEEKDAYS[lang][t.weekday()]
    if lang == "pt":
        return f"{wd}, {t:%d/%m/%Y} às {t:%H:%M}"
    return f"{wd}, {_MONTHS_EN[t.month - 1]} {t.day}, {t.year} at {t:%H:%M}"


def _when_lines(settings: Settings, b: Booking, lang: str) -> str:
    line = f"{fmt_when(b.start_at, settings.tz, lang)} ({settings.timezone})"
    if b.guest_tz and b.guest_tz != settings.timezone:
        g = ZoneInfo(b.guest_tz)
        mine = "no seu fuso" if lang == "pt" else "your time"
        line += f"\n= {fmt_when(b.start_at, g, lang)} ({mine}, {b.guest_tz})"
    return line


def _meeting(settings: Settings, lang: str) -> str:
    if settings.meeting_url:
        return settings.meeting_url
    return "o link chega por e-mail antes da conversa" if lang == "pt" else "the link arrives by e-mail before the call"


def invite(settings: Settings, b: Booking, for_owner: bool, cancel: bool = False, link: str | None = None) -> str:
    cal = icalendar.Calendar()
    cal.add("prodid", "-//vgmelo.tech//vgmelo-agenda//EN")
    cal.add("version", "2.0")
    cal.add("method", "CANCEL" if cancel else "PUBLISH")
    ev = icalendar.Event()
    ev.add("uid", f"{b.id}@vgmelo.tech")
    ev.add("sequence", b.sequence + (1 if cancel else 0))
    ev.add("dtstamp", datetime.now(UTC))
    ev.add("dtstart", b.start_at.astimezone(UTC))
    ev.add("dtend", b.end_at.astimezone(UTC))
    lang = "pt" if for_owner else b.lang
    label = KIND_LABEL[lang][b.kind]
    if for_owner:
        ev.add("summary", f"{label.capitalize()}: {b.name}" + (f" ({b.company})" if b.company else ""))
    else:
        ev.add("summary", f"{settings.owner_name} — {label}")
    if settings.meeting_url:
        ev.add("location", settings.meeting_url)
    desc = [_meeting(settings, lang)]
    if link:
        desc.append(link)
    ev.add("description", "\n".join(desc))
    ev.add("organizer", f"mailto:{parseaddr(settings.email_from)[1] or 'no-reply@vgmelo.tech'}")
    ev.add("status", "CANCELLED" if cancel else "CONFIRMED")
    cal.add_component(ev)
    return str(cal.to_ical().decode())


def pending(settings: Settings, b: Booking, token: str) -> Email:
    label = KIND_LABEL[b.lang][b.kind]
    url = manage_url(settings, b.lang, token)
    if b.lang == "pt":
        subject = f"Confirme sua conversa com {settings.owner_name}"
        text = (
            f"Recebemos um pedido de conversa ({label}, {b.duration} min) com {settings.owner_name}:\n\n"
            f"{_when_lines(settings, b, 'pt')}\n\n"
            f"Para confirmar, abra o link em até {settings.hold_minutes} minutos:\n{url}\n\n"
            "Se não foi você, é só ignorar: sem confirmação o horário é liberado sozinho.\n"
        )
    else:
        subject = f"Confirm your call with {settings.owner_name}"
        text = (
            f"We got a request for a call ({label}, {b.duration} min) with {settings.owner_name}:\n\n"
            f"{_when_lines(settings, b, 'en')}\n\n"
            f"To confirm it, open this link within {settings.hold_minutes} minutes:\n{url}\n\n"
            "If this wasn't you, just ignore it: without confirmation the slot is released on its own.\n"
        )
    return Email(b.email, subject, text)


def confirmed_guest(settings: Settings, b: Booking, token: str, rescheduled: bool = False) -> Email:
    label = KIND_LABEL[b.lang][b.kind]
    url = manage_url(settings, b.lang, token)
    if b.lang == "pt":
        subject = ("Conversa remarcada" if rescheduled else "Conversa confirmada") + f" com {settings.owner_name}"
        text = (
            f"{'Remarcada' if rescheduled else 'Confirmada'}: conversa ({label}, {b.duration} min) "
            f"com {settings.owner_name}.\n\n{_when_lines(settings, b, 'pt')}\n\n"
            f"Onde: {_meeting(settings, 'pt')}\n\n"
            f"Para cancelar ou remarcar (até {settings.change_cutoff_hours}h antes):\n{url}\n\n"
            "O convite vai anexado (invite.ics).\n"
        )
    else:
        subject = ("Call rescheduled" if rescheduled else "Call confirmed") + f" with {settings.owner_name}"
        text = (
            f"{'Rescheduled' if rescheduled else 'Confirmed'}: call ({label}, {b.duration} min) "
            f"with {settings.owner_name}.\n\n{_when_lines(settings, b, 'en')}\n\n"
            f"Where: {_meeting(settings, 'en')}\n\n"
            f"To cancel or reschedule (up to {settings.change_cutoff_hours}h before):\n{url}\n\n"
            "The invite is attached (invite.ics).\n"
        )
    return Email(b.email, subject, text, ics=invite(settings, b, for_owner=False, link=url))


def owner_notice(settings: Settings, b: Booking, owner_token: str, rescheduled: bool = False) -> Email:
    label = KIND_LABEL["pt"][b.kind]
    url = manage_url(settings, "pt", owner_token)
    head = "Remarcada" if rescheduled else "Nova conversa"
    text = (
        f"{head} ({label}, {b.duration} min), via {b.source}.\n\n"
        f"{fmt_when(b.start_at, settings.tz, 'pt')} ({settings.timezone})\n\n"
        f"Nome: {b.name}\nE-mail: {b.email}\nEmpresa: {b.company or '-'}\n"
        f"Fuso da pessoa: {b.guest_tz or '-'}\nIdioma: {b.lang}\n\nAssunto:\n{b.topic}\n\n"
        f"Cancelar (link só seu, a pessoa é avisada):\n{url}\n"
    )
    return Email(
        settings.owner_email,
        f"[agenda] {head}: {b.name} — {fmt_when(b.start_at, settings.tz, 'pt')}",
        text,
        reply_to=b.email,
        ics=invite(settings, b, for_owner=True, link=url),
    )


def cancelled_guest(settings: Settings, b: Booking) -> Email:
    label = KIND_LABEL[b.lang][b.kind]
    by_owner = b.cancelled_by == "owner"
    if b.lang == "pt":
        subject = f"Conversa cancelada com {settings.owner_name}"
        why = f"{settings.owner_name} precisou cancelar" if by_owner else "Cancelamento feito"
        text = (
            f"{why}: conversa ({label}, {b.duration} min) de {_when_lines(settings, b, 'pt')}.\n\n"
            f"Para marcar outro horário: {book_url(settings, 'pt')}\n"
        )
    else:
        subject = f"Call with {settings.owner_name} cancelled"
        why = f"{settings.owner_name} had to cancel" if by_owner else "Cancelled"
        text = (
            f"{why}: call ({label}, {b.duration} min) on {_when_lines(settings, b, 'en')}.\n\n"
            f"To book another time: {book_url(settings, 'en')}\n"
        )
    return Email(b.email, subject, text, ics=invite(settings, b, for_owner=False, cancel=True))


def cancelled_owner(settings: Settings, b: Booking) -> Email:
    who = "você" if b.cancelled_by == "owner" else b.name
    text = (
        f"Cancelada por {who}: {KIND_LABEL['pt'][b.kind]} com {b.name} <{b.email}>, "
        f"{fmt_when(b.start_at, settings.tz, 'pt')} ({settings.timezone}).\n"
    )
    return Email(
        settings.owner_email,
        f"[agenda] Cancelada: {b.name} — {fmt_when(b.start_at, settings.tz, 'pt')}",
        text,
        reply_to=b.email,
        ics=invite(settings, b, for_owner=True, cancel=True),
    )
