"""Authentication primitives for the local control panel, without I/O."""

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Literal


LOCAL_HOSTS = frozenset({"127.0.0.1:8090", "localhost:8090"})
# One machine label under one tailnet label, as Tailscale Serve presents it (default HTTPS port only).
TAILNET_HOST = re.compile(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net(?::443)?", re.IGNORECASE)


def allowed_host(host_header: str) -> bool:
    """Reject DNS-rebinding names before any cookie, CSRF or PIN logic sees the request."""
    return host_header in LOCAL_HOSTS or TAILNET_HOST.fullmatch(host_header) is not None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Look up an HTTP header without requiring a specialized mapping."""
    name = name.lower()
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def classify(
    client_host: str, host_header: str, headers: Mapping[str, str],
) -> Literal["local", "tailnet"]:
    if (
        client_host in {"127.0.0.1", "::1"}
        and host_header in LOCAL_HOSTS
        and all(_header(headers, name) is None for name in (
            "Tailscale-User-Login", "X-Forwarded-For", "X-Forwarded-Host",
            "X-Forwarded-Proto", "Forwarded",
        ))
    ):
        return "local"
    return "tailnet"


def identity(headers: Mapping[str, str]) -> str:
    login = _header(headers, "Tailscale-User-Login")
    return "unknown" if login is None else login


def valid_pin(pin: str) -> bool:
    return re.fullmatch(r"\d{6,12}", pin) is not None


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = stored.split("$")
        if algorithm != "scrypt" or len(salt_hex) != 32 or len(digest_hex) != 128:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if len(salt) != 16 or len(expected) != 64:
            return False
    except ValueError:
        return False
    actual = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return hmac.compare_digest(actual, expected)


class Lockout:
    """Track escalating locks in a bounded LRU, retaining the global '*' bucket."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        # Each entry holds failures, previous lock duration, and expiry.
        self._attempts: OrderedDict[str, tuple[int, float, float]] = OrderedDict()

    def check(self, ident: str) -> float:
        with self._lock:
            entry = self._attempts.get(ident)
            if entry is None:
                return 0.0
            self._attempts.move_to_end(ident)
            return max(0.0, entry[2] - self._clock())

    def attempt(self, ident: str) -> float:
        """Reserve a guess atomically; call success after valid authentication.

        Do not call fail for a rejected guess reserved by this method.
        """
        with self._lock:
            return self._attempt_locked(ident)

    def fail(self, ident: str) -> None:
        with self._lock:
            self._attempt_locked(ident)

    def _attempt_locked(self, ident: str) -> float:
        """Check and pre-count one attempt while the caller holds _lock."""
        now = self._clock()
        failures, duration, until = self._attempts.get(ident, (0, 0.0, 0.0))
        if ident in self._attempts:
            self._attempts.move_to_end(ident)
        if until > now:
            return until - now
        failures += 1
        if failures >= 5:
            duration = min(duration * 2, 86400.0) if duration else 900.0
            failures = 0
            until = now + duration
        self._attempts[ident] = (failures, duration, until)
        if len(self._attempts) > 256:
            oldest = next(key for key in self._attempts if key != "*")
            del self._attempts[oldest]
        return 0.0

    def success(self, ident: str) -> None:
        with self._lock:
            self._attempts.pop(ident, None)


class Sessions:
    """Keep opaque sessions in instance memory, with an injected wall clock."""

    def __init__(self, clock: Callable[[], float] = time.time, ttl: float = 43200):
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}

    def create(self, kind: str) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[sid] = (kind, self._clock() + self._ttl)
        return sid

    def get(self, sid: str) -> str | None:
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            kind, expires = session
            if self._clock() >= expires:
                self._sessions.pop(sid, None)
                return None
            return kind

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def revoke_kind(self, kind: str) -> None:
        """Log out every session of one kind, e.g. all phones after a PIN change."""
        with self._lock:
            for sid in [sid for sid, (value, _) in self._sessions.items() if value == kind]:
                del self._sessions[sid]


def effective_scheme(
    request_scheme: str, host_header: str, headers: Mapping[str, str],
) -> str:
    forwarded = _header(headers, "X-Forwarded-Proto")
    if forwarded is not None:
        return forwarded
    if host_header.endswith(".ts.net"):
        return "https"
    return request_scheme


def csrf_ok(
    method: str, headers: Mapping[str, str], scheme: str, host_header: str,
) -> bool:
    if method.upper() not in {"POST", "PUT", "DELETE"}:
        return True
    if _header(headers, "X-Panel-Request") != "1":
        return False
    origin = _header(headers, "Origin")
    return origin is None or origin == f"{scheme}://{host_header}"
