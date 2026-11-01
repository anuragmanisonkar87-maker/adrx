"""Keeps a free website awake.

Render's free plan puts a site to sleep after 15 minutes without visitors, and the next visitor then waits
about a minute while it starts again. With KEEP_AWAKE=on the app opens its own public address every few
minutes (KEEP_AWAKE_MINUTES, default 10). The visit leaves the server and comes back in through Render like
any visitor's, so the site never sits idle for 15 minutes. Render tells the app its address in
RENDER_EXTERNAL_URL; on other hosts set KEEP_AWAKE_URL.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import httpx

from app import __version__
from app.config import Settings

log = logging.getLogger("nmc.keep_awake")

HEALTH_PATH = "/api/health"  # quick to answer, and never counted toward the visitor limits

_http_get = httpx.get  # looked up on every visit, so tests can replace it


def health_url(settings: Settings) -> str | None:
    return f"{settings.keep_awake_url}{HEALTH_PATH}" if settings.keep_awake_url else None


class KeepAwake:
    """A background thread that opens ``url`` every ``every_seconds``, and again sooner after a failed visit."""

    def __init__(self, url: str, every_seconds: float, retry_seconds: float = 60.0, first_seconds: float = 60.0,
                 get: Callable[..., httpx.Response] | None = None) -> None:
        self.url = url
        self.every_seconds = every_seconds
        self.retry_seconds = min(retry_seconds, every_seconds)
        self.first_seconds = first_seconds  # the first visit, shortly after start-up, shows in the log
        self.visits = 0  # visits the site answered
        self.failures = 0  # failed visits in a row
        self._get = get
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="keep-awake", daemon=True)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> "KeepAwake":
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def visit(self) -> bool:
        """Opens the address once. True when the site answered."""
        try:
            response = (self._get or _http_get)(self.url, timeout=60, follow_redirects=True,
                                                headers={"User-Agent": f"AdRx-keep-awake/{__version__}"})
            ok, problem = response.is_success, f"HTTP {response.status_code}"
        except Exception as exc:  # no connection, time-out, bad address
            ok, problem = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        if ok:
            if self.failures or not self.visits:
                log.info("Keep-awake: %s answered; next visit in %.0f minutes", self.url, self.every_seconds / 60)
            self.visits += 1
            self.failures = 0
        else:
            self.failures += 1
            if self.failures <= 3 or self.failures % 60 == 0:  # don't fill the log during a long outage
                log.warning("Keep-awake: %s did not answer (%s); trying again in %.0f seconds",
                            self.url, problem, self.retry_seconds)
        return ok

    def _run(self) -> None:
        wait = self.first_seconds
        while not self._stop.wait(wait):
            wait = self.every_seconds if self.visit() else self.retry_seconds


_current: KeepAwake | None = None
_lock = threading.Lock()


def start(settings: Settings) -> KeepAwake | None:
    """Starts the visits when KEEP_AWAKE is on. Called once when the app starts."""
    global _current
    if not settings.keep_awake:
        return None
    url = health_url(settings)
    if not url:
        log.warning("KEEP_AWAKE is on, but the site's address is unknown: set KEEP_AWAKE_URL to it")
        return None
    with _lock:
        if _current is None or not _current.running:
            _current = KeepAwake(url, settings.keep_awake_minutes * 60).start()
            log.info("Keep-awake is on: the site opens %s every %d minutes so it never falls asleep",
                     url, settings.keep_awake_minutes)
        return _current


def stop() -> None:
    global _current
    with _lock:
        if _current is not None:
            _current.stop()
            _current = None


def running() -> bool:
    current = _current
    return current is not None and current.running
