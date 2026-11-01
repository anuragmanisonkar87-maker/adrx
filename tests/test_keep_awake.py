"""KEEP_AWAKE: the free website opens its own address every few minutes so Render never puts it to sleep."""

import time

import httpx
from fastapi.testclient import TestClient

from app import keep_awake
from app.config import Settings
from app.main import app

URL = "https://adrx.onrender.com/api/health"


class FakeSite:
    """Answers the visits in order (status codes or errors); the last answer repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, request=httpx.Request("GET", url))


class FakeClock:
    """Stands in for the thread's stop event: records each wait and says "stop" after ``rounds`` waits."""

    def __init__(self, rounds: int):
        self.rounds = rounds
        self.waits: list[float] = []

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return len(self.waits) > self.rounds

    def set(self) -> None:
        pass


def test_off_unless_switched_on(monkeypatch):
    s = Settings.from_env()
    assert (s.keep_awake, s.keep_awake_url, s.keep_awake_minutes) == (False, None, 10)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://adrx.onrender.com")
    assert keep_awake.start(Settings.from_env()) is None  # Render alone does not switch it on


def test_address_comes_from_render_or_keep_awake_url(monkeypatch):
    monkeypatch.setenv("KEEP_AWAKE", "on")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://adrx.onrender.com/")
    s = Settings.from_env()
    assert s.keep_awake and keep_awake.health_url(s) == URL
    monkeypatch.setenv("KEEP_AWAKE_URL", "check.example.in")  # another host: its own address, https assumed
    assert keep_awake.health_url(Settings.from_env()) == "https://check.example.in/api/health"


def test_minutes_stay_inside_renders_15_idle_minutes(monkeypatch):
    for given, used in (("30", 14), ("0", 1), ("5", 5), ("soon", 10)):
        monkeypatch.setenv("KEEP_AWAKE_MINUTES", given)
        assert Settings.from_env().keep_awake_minutes == used


def test_no_address_means_no_visits(monkeypatch, caplog):
    monkeypatch.setenv("KEEP_AWAKE", "on")
    assert keep_awake.start(Settings.from_env()) is None
    assert "KEEP_AWAKE_URL" in caplog.text


def test_visit_opens_the_health_page():
    site = FakeSite(200)
    waker = keep_awake.KeepAwake(URL, every_seconds=600, get=site.get)
    assert waker.visit() is True and waker.visits == 1
    url, kwargs = site.calls[0]
    assert url == URL and kwargs["timeout"] >= 30 and kwargs["headers"]["User-Agent"].startswith("AdRx-keep-awake")


def test_failed_visits_are_retried_sooner(caplog):
    site = FakeSite(200, httpx.ConnectError("no route"), 503, 200)
    waker = keep_awake.KeepAwake(URL, every_seconds=600, retry_seconds=60, first_seconds=45, get=site.get)
    clock = FakeClock(rounds=5)
    waker._stop = clock
    waker._run()
    assert len(site.calls) == 5
    # first visit soon after start-up; a full interval after each answered visit, a minute after a failure
    assert clock.waits == [45, 600, 60, 60, 600, 600]
    assert waker.failures == 0 and waker.visits == 3
    assert "did not answer (ConnectError: no route)" in caplog.text and "HTTP 503" in caplog.text


def test_thread_starts_and_stops():
    site = FakeSite(200)
    waker = keep_awake.KeepAwake(URL, every_seconds=0.05, first_seconds=0.01, get=site.get).start()
    deadline = time.monotonic() + 5
    while len(site.calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert waker.running and len(site.calls) >= 3
    waker.stop()
    assert not waker.running


def test_the_app_starts_it_and_health_says_so(monkeypatch):
    site = FakeSite(200)
    monkeypatch.setattr(keep_awake, "_http_get", site.get)  # no real visits from the tests
    monkeypatch.setenv("KEEP_AWAKE", "on")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://adrx.onrender.com")
    with TestClient(app) as client:  # runs the app's start-up and shut-down
        assert client.get("/api/health").json()["keep_awake"] is True
        assert keep_awake._current.url == URL and keep_awake._current.every_seconds == 600
    assert keep_awake.running() is False
    monkeypatch.delenv("KEEP_AWAKE")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["keep_awake"] is False
