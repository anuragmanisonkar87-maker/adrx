"""Session cookies, CSRF tokens and limits on failed sign-ins."""

from __future__ import annotations

import bisect
import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import Response

COOKIE = "adrx_session"
SECURE_COOKIE = "__Host-adrx_session"  # the __Host- prefix: HTTPS only, this site only, the whole site
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def new_token() -> tuple[str, str]:
    """A random session token for the cookie, and the SHA-256 hash that is stored instead of it."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def session_token(request: Request) -> str | None:
    return request.cookies.get(SECURE_COOKIE) or request.cookies.get(COOKIE) or None


def set_session_cookie(response: Response, request: Request, token: str, max_age: int) -> None:
    secure = request.url.scheme == "https"
    response.set_cookie(SECURE_COOKIE if secure else COOKIE, token, max_age=max_age, path="/", httponly=True,
                        secure=secure, samesite="lax")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")
    response.delete_cookie(SECURE_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def csrf_ok(request: Request, expected: str) -> bool:
    given = request.headers.get(CSRF_HEADER, "")
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


def same_origin(request: Request) -> bool:
    """False when the browser says the request comes from another site (Origin header)."""
    origin = request.headers.get("origin")
    if not origin:
        return True  # not a browser, or an old one; the CSRF token still protects signed-in requests
    return urlsplit(origin).netloc.lower() == (request.headers.get("host") or "").lower()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def safe_next(path: str | None) -> str:
    """Where to go after signing in: only a page on this site."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path or "://" in path:
        return "/"
    return path


class SignInLimits:
    """Failed sign-ins in the last 15 minutes, kept in memory: 5 for a username from one address, 20 from one
    address, and 50 for one username from anywhere (which slows a spread-out attack on one account)."""

    WINDOW = 900
    LIMITS = (("pair", 5), ("ip", 20), ("user", 50))

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    @staticmethod
    def _keys(ip: str, username: str) -> dict[str, str]:
        return {"pair": f"{username}|{ip}", "ip": f"ip:{ip}", "user": f"user:{username}"}

    def wait_seconds(self, ip: str, username: str, now: float | None = None) -> int | None:
        """None when a sign-in may be tried, else how long to wait."""
        now = time.time() if now is None else now
        with self._lock:
            for kind, limit in self.LIMITS:
                times = self._recent(self._keys(ip, username)[kind], now)
                if len(times) >= limit:
                    return int(times[len(times) - limit] + self.WINDOW - now) + 1
        return None

    def failed(self, ip: str, username: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            for key in self._keys(ip, username).values():
                self._recent(key, now).append(now)
            if len(self._failures) > 50_000:
                for key in [k for k, v in self._failures.items() if not v or v[-1] < now - self.WINDOW]:
                    del self._failures[key]

    def succeeded(self, ip: str, username: str) -> None:
        with self._lock:
            self._failures.pop(self._keys(ip, username)["pair"], None)

    def _recent(self, key: str, now: float) -> list[float]:
        times = self._failures.setdefault(key, [])
        del times[:bisect.bisect_right(times, now - self.WINDOW)]
        return times

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


sign_in_limits = SignInLimits()
