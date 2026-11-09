"""Transactional e-mail through the Resend HTTP API (plain text, optional .ics attachment)."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    reply_to: str | None = None
    ics: str | None = None  # iCalendar body, attached as invite.ics


class Mailer:
    def __init__(self, api_key: str, sender: str, http: httpx.Client) -> None:
        self._key, self._from, self._http = api_key, sender, http

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def send(self, mail: Email) -> bool:
        if not self._key or not mail.to:
            return False
        body: dict[str, object] = {
            "from": self._from,
            "to": [mail.to],
            "subject": " ".join(mail.subject.split())[:200],
            "text": mail.text,
        }
        if mail.reply_to:
            body["reply_to"] = mail.reply_to
        if mail.ics:
            body["attachments"] = [
                {
                    "filename": "invite.ics",
                    "content": base64.b64encode(mail.ics.encode()).decode(),
                    "content_type": "text/calendar; charset=utf-8",
                }
            ]
        try:
            resp = self._http.post(RESEND_URL, json=body, headers={"Authorization": f"Bearer {self._key}"}, timeout=10)
        except httpx.HTTPError as exc:
            logger.warning("resend request failed: %s", type(exc).__name__)
            return False
        if resp.status_code >= 300:
            logger.warning("resend refused the message: status %s", resp.status_code)
            return False
        return True
