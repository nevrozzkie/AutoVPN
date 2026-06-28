"""Security helpers: admin password hashing, login rate limiting,
CSRF (origin) checks and security headers.

The admin password used to be stored in plaintext and compared directly.
It is now stored as a salted scrypt hash. For backwards compatibility and
for bootstrap via the ``ADMIN_PASSWORD`` environment variable, a plaintext
stored value is still accepted by :func:`verify_password` (it is treated as
a legacy/bootstrap value and verified with a constant-time comparison).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from threading import Lock
from urllib.parse import urlsplit

# scrypt parameters (n, r, p). n=2**15 is a reasonable interactive cost.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_HASH_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    """Return a ``scrypt$n$r$p$salt$hash`` string for ``password``."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=132 * 1024 * 1024,
    )
    return "{}${}${}${}${}${}".format(
        _HASH_PREFIX,
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    )


def is_hashed(stored: str) -> bool:
    return stored.startswith(_HASH_PREFIX + "$")


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of ``password`` against ``stored``.

    ``stored`` may be a scrypt hash (produced by :func:`hash_password`) or a
    legacy/bootstrap plaintext value (e.g. from the ADMIN_PASSWORD env var).
    """
    if not stored:
        return False
    if not is_hashed(stored):
        # Legacy / bootstrap plaintext value.
        return hmac.compare_digest(password, stored)
    try:
        _prefix, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=132 * 1024 * 1024,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


# --------------------------------------------------------------------------
# Login rate limiting (in-memory, per client IP). Suitable for the
# single-process uvicorn deployment used here.
# --------------------------------------------------------------------------


class LoginRateLimiter:
    def __init__(self, max_failures: int = 10, window_seconds: int = 300, block_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = Lock()

    def _now(self) -> float:
        return time.monotonic()

    def retry_after(self, key: str) -> int:
        """Return seconds to wait if ``key`` is currently blocked, else 0."""
        now = self._now()
        with self._lock:
            until = self._blocked_until.get(key)
            if until and until > now:
                return int(until - now) + 1
            if until:
                self._blocked_until.pop(key, None)
        return 0

    def register_failure(self, key: str) -> None:
        now = self._now()
        with self._lock:
            bucket = [t for t in self._failures.get(key, []) if now - t < self.window_seconds]
            bucket.append(now)
            self._failures[key] = bucket
            if len(bucket) >= self.max_failures:
                self._blocked_until[key] = now + self.block_seconds
                self._failures[key] = []

    def register_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._blocked_until.pop(key, None)


login_rate_limiter = LoginRateLimiter()


def client_key(request) -> str:  # type: ignore[no-untyped-def]
    """Best-effort client identifier honouring the nginx X-Forwarded-For header."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# --------------------------------------------------------------------------
# CSRF protection via Origin / Referer checking. With HTTP Basic auth there
# is no session cookie, but browsers still auto-attach cached credentials, so
# state-changing requests must originate from the panel's own host.
# --------------------------------------------------------------------------

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _host_of(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    return parts.netloc or ""


def is_same_origin_request(request) -> bool:  # type: ignore[no-untyped-def]
    """True if a state-changing request is safe from a CSRF standpoint.

    Strategy:
    * Safe methods are always allowed.
    * If an Origin header is present, its host must equal the request host.
    * Otherwise, if a Referer header is present, its host must equal the host.
    * If neither is present (non-browser clients), allow: browsers always
      send at least one of these for cross-site state-changing requests, so
      their absence is not a browser-driven CSRF vector.
    """
    if request.method in _SAFE_METHODS:
        return True
    expected_host = request.headers.get("host", "")
    origin = request.headers.get("origin", "")
    if origin:
        return _host_of(origin) == expected_host
    referer = request.headers.get("referer", "")
    if referer:
        return _host_of(referer) == expected_host
    return True


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Inline scripts/styles and onclick handlers are used in templates, so
    # 'unsafe-inline' is required for now. frame-ancestors 'none' is the
    # primary clickjacking defence.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    ),
}
