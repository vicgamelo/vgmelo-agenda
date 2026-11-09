"""Tokens, keyed hashes, client IP and an in-memory token bucket."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import Network


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_id() -> str:
    return secrets.token_hex(16)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Keyring:
    def __init__(self, secret_key: str) -> None:
        self._key = hashlib.sha256(b"vgmelo-agenda/mac\0" + secret_key.encode()).digest()

    def mac(self, value: str) -> str:
        """Keyed hash for low-entropy values (IPs, e-mails) kept in counters."""
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()


def client_ip(peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]) -> str:
    """The peer address, unless it is a trusted proxy: then the right-most untrusted hop of X-Forwarded-For."""

    def parse(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(value.strip())
        except ValueError:
            return None

    def is_trusted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(addr in net for net in trusted)

    addr = parse(peer or "")
    if addr is None:
        return peer or "unknown"
    if not trusted or not is_trusted(addr) or not forwarded_for:
        return str(addr)
    for hop in reversed(forwarded_for.split(",")):
        hop_addr = parse(hop)
        if hop_addr is None:
            break
        if not is_trusted(hop_addr):
            return str(hop_addr)
        addr = hop_addr
    return str(addr)


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucket:
    """Per-key token bucket. Idle buckets are pruned, so memory stays bounded under a flood of addresses."""

    def __init__(
        self, rate_per_minute: float, burst: int, max_tracked: int = 50_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.rate, self.burst, self.max_tracked, self.clock = rate_per_minute / 60, float(burst), max_tracked, clock
        self.buckets: dict[str, _Bucket] = {}

    def hit(self, key: str) -> float:
        """0 when allowed, else seconds until the next token."""
        now = self.clock()
        b = self.buckets.get(key)
        if b is None:
            if len(self.buckets) >= self.max_tracked:
                self._prune(now)
            b = self.buckets[key] = _Bucket(self.burst, now)
        else:
            b.tokens = min(self.burst, b.tokens + (now - b.updated) * self.rate)
            b.updated = now
        if b.tokens >= 1:
            b.tokens -= 1
            return 0.0
        return (1 - b.tokens) / self.rate

    def _prune(self, now: float) -> None:
        full_after = self.burst / self.rate
        self.buckets = {k: b for k, b in self.buckets.items() if now - b.updated < full_after}
        if len(self.buckets) >= self.max_tracked:
            keep = sorted(self.buckets.items(), key=lambda kv: kv[1].updated)[len(self.buckets) // 2 :]
            self.buckets = dict(keep)
