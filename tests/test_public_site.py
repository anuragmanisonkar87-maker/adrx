"""Public-website mode: visitor limits, busy server, hidden setup details, AI review switched off."""

import re
import threading
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine import ai_review
from app.main import VisitorLimits, _slots, app

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
client = TestClient(app)
AD = "Mumbai's No.1 skin clinic. 100% guaranteed results!"


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("PUBLIC_SITE", "true")
    monkeypatch.setenv("AI_REVIEW", "off")


def test_visitor_limit_per_hour(monkeypatch, public):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "2")
    assert client.post("/api/check", data={"text": AD}).status_code == 200
    assert client.post("/api/check", data={"text": AD}).status_code == 200
    r = client.post("/api/check", data={"text": AD})
    assert r.status_code == 429
    assert "try again in about 60 minutes" in r.json()["detail"]
    assert 3500 < int(r.headers["retry-after"]) <= 3601


def test_empty_requests_do_not_count(monkeypatch, public):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "1")
    assert client.post("/api/check", data={"text": "  "}).status_code == 400
    assert client.post("/api/check", data={"text": AD}).status_code == 200


def test_visitor_limits_hour_and_day_windows():
    limits = VisitorLimits()
    t = 1_000_000.0
    for i in range(3):
        assert limits.take("a", 3, 5, now=t + i) is None
    assert limits.take("a", 3, 5, now=t + 10) == 3591  # the first check leaves the hour window at t + 3600
    assert limits.take("b", 3, 5, now=t + 10) is None  # other visitors are not affected
    assert limits.take("a", 3, 5, now=t + 3601) is None
    assert limits.take("a", 3, 5, now=t + 3602) is None
    assert limits.take("a", 3, 5, now=t + 3603) == 86400 - 3603 + 1  # 5 a day used up
    assert limits.take("a", 3, 5, now=t + 86401) is None


def test_busy_server_answers_503_for_files(monkeypatch, public, samples):
    monkeypatch.setenv("MAX_PARALLEL_CHECKS", "1")
    monkeypatch.setenv("QUEUE_WAIT_SECONDS", "0")
    slot = _slots.setdefault(1, threading.BoundedSemaphore(1))
    assert slot.acquire(timeout=5)  # another file check is running
    try:
        with open(samples / "clinic_website.pdf", "rb") as fh:
            r = client.post("/api/check", files={"file": ("ad.pdf", fh, "application/pdf")})
        assert r.status_code == 503 and "busy" in r.json()["detail"]
        assert client.post("/api/check", data={"text": AD}).status_code == 200  # text checks don't queue
    finally:
        slot.release()
    with open(samples / "clinic_website.pdf", "rb") as fh:
        assert client.post("/api/check", files={"file": ("ad.pdf", fh, "application/pdf")}).status_code == 200


def test_api_pages_hidden_on_public_site(monkeypatch):
    assert client.get("/docs").status_code == 200
    monkeypatch.setenv("PUBLIC_SITE", "true")
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert client.get("/").status_code == 200


def test_health_tells_the_page_it_is_public(monkeypatch, public):
    monkeypatch.setenv("MAX_MEDIA_SECONDS", "180")
    body = client.get("/api/health?verify_ai=true").json()
    assert body["public"] is True and body["ai_enabled"] is False and body["ai_status"] == "off"
    assert body["max_media_minutes"] == 3


def test_ai_review_off_even_with_a_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert Settings.from_env().ai_enabled is True
    monkeypatch.setenv("AI_REVIEW", "off")
    settings = Settings.from_env()
    assert settings.ai_enabled is False
    assert ai_review.verify_access(settings)["message"].startswith("AI_REVIEW is off")
    body = client.post("/api/check", data={"text": AD}).json()
    assert body["analysis"]["mode"] == "rules"


def test_page_hides_setup_hints_from_visitors():
    script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "else if (h.public)" in script and "media-limit" in script
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="privacy-note"' in page and 'id="media-limit"' in page


# --- deployment files --------------------------------------------------------------------------------

def _env_lines(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if re.match(r"^[A-Z_]+=", line))


def test_site_settings_are_safe_for_a_public_website():
    env = _env_lines(DEPLOY / ".env.example")
    assert env["PUBLIC_SITE"] == "true" and env["AI_REVIEW"] == "off"
    assert int(env["RATE_LIMIT_PER_HOUR"]) > 0 and int(env["RATE_LIMIT_PER_DAY"]) > 0
    assert int(env["MAX_PARALLEL_CHECKS"]) > 0 and int(env["MAX_UPLOAD_MB"]) <= 200
    assert env["LOG_FILE"] == ""
    proxy_limit = int(env["PROXY_MAX_BODY"].removesuffix("MB"))
    assert proxy_limit > int(env["MAX_UPLOAD_MB"])  # the app, not the proxy, explains a too-big file


def test_compose_file_runs_app_behind_caddy():
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    app_service, caddy = compose["services"]["app"], compose["services"]["caddy"]
    assert "ports" not in app_service  # only Caddy is reachable from outside
    assert app_service["environment"]["FORWARDED_ALLOW_IPS"] == "*"  # real visitor IPs for the limits
    assert app_service["build"]["args"]["PRELOAD_WHISPER"] == "1"
    assert {"80:80", "443:443"} <= set(caddy["ports"])
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]


def test_caddyfile_and_scripts():
    caddyfile = (DEPLOY / "Caddyfile").read_text()
    assert "{$SITE_ADDRESS" in caddyfile and "reverse_proxy {$APP_UPSTREAM:app:8000}" in caddyfile
    assert "max_size {$PROXY_MAX_BODY" in caddyfile
    script = (DEPLOY / "setup-server.sh").read_bytes()
    assert b"\r\n" not in script, "shell scripts need LF line endings"
    assert script.startswith(b"#!/usr/bin/env bash") and b"set -euo pipefail" in script
    assert "deploy/" in (ROOT / ".dockerignore").read_text().split()


# --- free hosting on Render --------------------------------------------------------------------------

def _render_env() -> dict:
    service = yaml.safe_load((ROOT / "render.yaml").read_text())["services"][0]
    assert service["plan"] == "free" and service["runtime"] == "docker" and service["healthCheckPath"] == "/api/health"
    return {item["key"]: item for item in service["envVars"]}


def test_render_blueprint_fits_the_free_plan():
    env = {key: item.get("value") for key, item in _render_env().items()}
    assert env["PUBLIC_SITE"] == "true" and env["AI_REVIEW"] == "off"
    assert env["MAX_PARALLEL_CHECKS"] == "1" and env["OCR_FRAME_WORKERS"] == "1"  # 512 MB of memory
    assert env["TRANSCRIBE_BACKEND"] == "openai" and "groq" in env["OPENAI_BASE_URL"]
    assert int(env["MAX_UPLOAD_MB"]) <= 50 and int(env["MAX_MEDIA_SECONDS"]) <= 180


def test_render_asks_for_the_speech_key_instead_of_storing_it():
    key = _render_env()["OPENAI_API_KEY"]
    assert key == {"key": "OPENAI_API_KEY", "sync": False}


def test_render_settings_are_read(monkeypatch):
    for key, item in _render_env().items():
        if "value" in item:
            monkeypatch.setenv(key, str(item["value"]))
    s = Settings.from_env()
    assert (s.ocr_frame_side, s.ocr_frame_workers, s.max_video_frames, s.max_parallel_checks) == (1280, 1, 8, 1)
    assert s.public_site and not s.ai_enabled and s.transcribe_backend == "openai"


def test_privacy_note_names_the_outside_speech_service(monkeypatch, public):
    monkeypatch.setenv("TRANSCRIBE_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
    assert client.get("/api/health").json()["speech_service"] is None  # no key: speech is off
    monkeypatch.setenv("OPENAI_API_KEY", "gsk_test")
    assert client.get("/api/health").json()["speech_service"] == "api.groq.com"
    monkeypatch.setenv("TRANSCRIBE_BACKEND", "local")
    assert client.get("/api/health").json()["speech_service"] is None


def test_visitors_get_a_plain_message_when_speech_is_not_set_up(monkeypatch, public, samples):
    pytest.importorskip("numpy")
    from tests.conftest import has_ffmpeg

    if not has_ffmpeg():
        pytest.skip("needs ffmpeg")
    monkeypatch.setenv("TRANSCRIBE_BACKEND", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with open(samples / "voice_note_3_english_compliant.ogg", "rb") as fh:
        r = client.post("/api/check", files={"file": ("note.ogg", fh, "audio/ogg")})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "Speech could not be turned into text on this website right now." in detail
    assert "OPENAI_API_KEY" not in detail and "TRANSCRIBE_BACKEND" not in detail
