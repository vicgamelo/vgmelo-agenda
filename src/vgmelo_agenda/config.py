"""Settings, all from the environment (see .env.example)."""

from __future__ import annotations

import ipaddress
import re
from datetime import time
from functools import cached_property
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

_WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "sqlite:///./agenda.db"
    # HMAC key for IPs and e-mails in rate-limit counters.
    secret_key: str = Field(min_length=32)

    # Public origin of the site; the API lives under the same origin at /api/agenda.
    site_url: str = "https://vgmelo.tech"
    allowed_origins: str = ""  # CSRF / cross-site check on POST. Defaults to site_url.
    trusted_proxies: str = ""  # CIDRs whose X-Forwarded-For is read

    # --- Business rules (confirmed by Victor, 2026-11-09) ---
    timezone: str = "America/Sao_Paulo"
    # Monday=0 ... Sunday=6
    weekdays: str = "0,1,2,3,4,5"
    windows: str = "08:00-10:00,12:00-14:00,18:00-20:00"
    step_minutes: int = Field(default=15, ge=5, le=60)
    buffer_minutes: int = Field(default=15, ge=0, le=120)
    min_notice_hours: int = Field(default=12, ge=0)
    max_days_ahead: int = Field(default=14, ge=1, le=60)
    max_per_day: int = Field(default=2, ge=1)
    hold_minutes: int = Field(default=30, ge=5)  # an unconfirmed booking holds the slot this long
    change_cutoff_hours: int = Field(default=2, ge=0)  # guests cancel / reschedule until this long before

    # Outlook (or any) calendar published as ICS; its busy times block slots. Empty: only our bookings count.
    ics_url: str = ""
    ics_refresh_minutes: int = Field(default=10, ge=1)
    # Without a successful fetch this recent, no slots are offered (no double booking on a stale copy).
    ics_max_stale_hours: int = Field(default=6, ge=1)

    resend_api_key: str = ""
    email_from: str = "vgmelo.tech <no-reply@vgmelo.tech>"
    owner_email: str = ""  # gets the notices and owner links; set only in the server env, never sent to the browser
    owner_name: str = "Victor Melo"
    meeting_url: str = ""  # fixed room; empty: "the link arrives by e-mail before the call"
    # Logs confirmation links when there is no Resend key. Local development only.
    dev_log_links: bool = False

    # --- Abuse limits ---
    booking_max_per_ip_hour: int = Field(default=5, ge=1)
    emails_per_address_day: int = Field(default=3, ge=1)
    max_future_per_email: int = Field(default=2, ge=1)
    max_pending_global: int = Field(default=6, ge=1)
    # Pending-confirmation e-mails per day. Resend's free plan (100/day) is shared with the portal.
    max_emails_day: int = Field(default=40, ge=1)
    manage_max_per_ip_hour: int = Field(default=30, ge=1)
    min_fill_seconds: int = Field(default=3, ge=0)
    max_body_bytes: int = Field(default=16 * 1024, ge=1024)
    # In-memory token bucket per IP for every /api/agenda request, and a tighter one for MCP.
    api_rate_per_minute: float = Field(default=120, gt=0)
    mcp_rate_per_minute: float = Field(default=30, gt=0)
    mcp_burst: int = Field(default=10, ge=1)
    mcp_allowed_hosts: str = ""  # Host headers accepted on /api/agenda/mcp (DNS rebinding); empty: any

    # --- Scheduling agent (OpenAI-compatible chat completions with tools) ---
    groq_api_key: str = ""  # a key only for this service, so it can't eat the other agents' limits
    groq_model: str = "openai/gpt-oss-20b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemma-4-26b-a4b-it:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    agent_daily_tokens: int = Field(default=100_000, ge=0)  # 0 turns the agent off
    agent_max_tokens: int = Field(default=400, ge=50)
    agent_message_max_chars: int = Field(default=500, ge=20)
    agent_max_turns: int = Field(default=10, ge=1)
    agent_max_per_ip_hour: int = Field(default=20, ge=1)
    agent_timeout_s: float = Field(default=20, gt=0)
    agent_max_conversations: int = Field(default=500, ge=1)

    @field_validator("site_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    @field_validator("windows")
    @classmethod
    def _valid_windows(cls, v: str) -> str:
        for item in _csv(v):
            m = _WINDOW.match(item)
            if not m or (int(m[3]), int(m[4])) <= (int(m[1]), int(m[2])):
                raise ValueError(f"bad window {item!r}, expected HH:MM-HH:MM")
        return v

    @field_validator("weekdays")
    @classmethod
    def _valid_weekdays(cls, v: str) -> str:
        if not all(d.isdigit() and 0 <= int(d) <= 6 for d in _csv(v)):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        return v

    @model_validator(mode="after")
    def _five_minute_grid(self) -> Settings:
        # Slots are claimed in 5-minute cells (see booking.claim).
        if self.step_minutes % 5 or self.buffer_minutes % 5:
            raise ValueError("step_minutes and buffer_minutes must be multiples of 5")
        return self

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @cached_property
    def day_windows(self) -> tuple[tuple[time, time], ...]:
        out = []
        for item in _csv(self.windows):
            start, end = item.split("-")
            out.append((time.fromisoformat(start), time.fromisoformat(end)))
        return tuple(sorted(out))

    @cached_property
    def open_weekdays(self) -> frozenset[int]:
        return frozenset(int(d) for d in _csv(self.weekdays))

    @cached_property
    def origins(self) -> frozenset[str]:
        return frozenset(o.rstrip("/") for o in (_csv(self.allowed_origins) or [self.site_url]))

    @cached_property
    def proxies(self) -> tuple[Network, ...]:
        return tuple(ipaddress.ip_network(c, strict=False) for c in _csv(self.trusted_proxies))

    @cached_property
    def mcp_hosts(self) -> list[str]:
        return _csv(self.mcp_allowed_hosts)

    @property
    def agent_providers(self) -> list[tuple[str, str, str, str]]:
        """(name, base_url, api_key, model) in fallback order, only the ones with a key."""
        all_ = [
            ("groq", self.groq_base_url, self.groq_api_key, self.groq_model),
            ("openrouter", self.openrouter_base_url, self.openrouter_api_key, self.openrouter_model),
        ]
        return [p for p in all_ if p[2]]

    @property
    def agent_enabled(self) -> bool:
        return bool(self.agent_providers) and self.agent_daily_tokens > 0
