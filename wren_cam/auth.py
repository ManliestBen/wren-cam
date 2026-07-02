"""Minimal admin auth: PBKDF2 password hashing + in-memory session tokens.

This is deliberately lightweight — it exists to keep casual visitors on the
LAN from deleting recordings or changing settings, not to withstand a
determined attacker. Sessions live in memory, so a process restart logs
everyone out (they just log back in).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

# Throwaway password used to seed the admin account on first run (see
# config.ConfigStore). It is intentionally a well-known placeholder — change it
# immediately after first launch via Settings → Change admin password. Your real
# password is then stored only as a hash in config.json, which is gitignored.
DEFAULT_ADMIN_PASSWORD = "0000"

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Return a self-describing hash string: 'pbkdf2_sha256$iters$salt$hash'."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash string."""
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


class SessionStore:
    """Thread-safe set of live session tokens. No persistence by design."""

    def __init__(self) -> None:
        self._tokens: set[str] = set()
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens.add(token)
        return token

    def is_valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return token in self._tokens

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._tokens.discard(token)

    def revoke_all(self) -> None:
        with self._lock:
            self._tokens.clear()


class LoginThrottle:
    """Per-key (usually per-IP) failed-login limiter.

    After `max_attempts` failures inside `window` seconds, the key is locked out
    for `lockout` seconds. A successful login clears the key. In-memory only, so
    it resets on restart — fine for a home LAN, and it turns a 4-digit PIN from
    seconds-to-brute-force into effectively unbrute-forceable.
    """

    def __init__(
        self, max_attempts: int = 5, window: float = 300.0, lockout: float = 300.0
    ) -> None:
        self.max_attempts = max_attempts
        self.window = window
        self.lockout = lockout
        self._lock = threading.Lock()
        # key -> {"fails": int, "first": monotonic ts, "locked_until": monotonic ts}
        self._state: dict[str, dict] = {}

    def seconds_until_unlocked(self, key: str) -> float:
        """0.0 if the key may attempt a login now, else seconds left on the lock."""
        now = time.monotonic()
        with self._lock:
            s = self._state.get(key)
            if s and s["locked_until"] > now:
                return s["locked_until"] - now
            return 0.0

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            s = self._state.get(key)
            if s is None or now - s["first"] > self.window:
                s = {"fails": 0, "first": now, "locked_until": 0.0}
            s["fails"] += 1
            if s["fails"] >= self.max_attempts:
                s["locked_until"] = now + self.lockout
            self._state[key] = s

    def record_success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)
