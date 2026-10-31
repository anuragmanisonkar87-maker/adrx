import base64

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import has_ffmpeg, has_tesseract

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["ai_enabled"] is False


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200 and "Check an ad before you post it." in r.text


def test_check_text():
    r = client.post("/api/check", data={"text": "Best skin clinic in Mumbai! 50% off on laser.", "channel": "social"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "non_compliant"
    assert body["source"]["kind"] == "text"


def test_check_requires_input():
    r = client.post("/api/check", data={"text": "   "})
    assert r.status_code == 400


def test_unsupported_file():
    r = client.post("/api/check", files={"file": ("data.bin", b"\x00\x01\x02\x03" * 10, "application/octet-stream")})
    assert r.status_code == 422
    assert "Unsupported file type" in r.json()["detail"]


def test_docx_upload(samples):
    with open(samples / "reel_script.docx", "rb") as fh:
        r = client.post("/api/check", files={"file": ("reel_script.docx", fh)})
    assert r.status_code == 200
    rules = {f["rule_id"] for f in r.json()["findings"]}
    assert {"testimonial_review", "inducement_offer", "success_rate_numbers"} <= rules


def test_text_pdf_upload(samples):
    with open(samples / "clinic_website.pdf", "rb") as fh:
        r = client.post("/api/check", files={"file": ("clinic_website.pdf", fh, "application/pdf")})
    body = r.json()
    assert r.status_code == 200 and body["source"]["kind"] == "pdf"
    assert "celebrity_endorsement" in {f["rule_id"] for f in body["findings"]}


@pytest.mark.skipif(not has_tesseract(), reason="tesseract not installed")
def test_scanned_pdf_uses_ocr(samples):
    with open(samples / "brochure_scanned.pdf", "rb") as fh:
        r = client.post("/api/check", files={"file": ("brochure.pdf", fh)}, data={"publisher": "hospital"})
    body = r.json()
    assert body["source"]["meta"]["ocr_pages"] == 1
    assert {"conceals_risk", "success_rate_numbers"} <= {f["rule_id"] for f in body["findings"]}


@pytest.mark.skipif(not has_tesseract(), reason="tesseract not installed")
def test_poster_with_caption(samples):
    with open(samples / "poster_en.png", "rb") as fh:
        r = client.post("/api/check", files={"file": ("poster.png", fh, "image/png")},
                        data={"text": "Hurry, limited slots! #hairtransplant"})
    body = r.json()
    assert body["text"].startswith("[Post text / caption]")
    rules = {f["rule_id"] for f in body["findings"]}
    assert {"superiority_claim", "guaranteed_outcome", "inducement_offer"} <= rules


@pytest.mark.skipif(not (has_ffmpeg() and has_tesseract()), reason="ffmpeg/tesseract not installed")
def test_video_reads_speech_and_onscreen_text(samples, monkeypatch):
    from app.extract import media
    from app.extract.transcribe import Transcript

    monkeypatch.setattr(media, "transcribe", lambda wav, lang, s: Transcript(
        "Hello, I am Doctor Rajesh Mehta. Book your appointment now, limited slots!", "en", "fake"))
    with open(samples / "reel_en.mp4", "rb") as fh:
        r = client.post("/api/check", files={"file": ("reel.mp4", fh, "video/mp4")})
    body = r.json()
    assert body["source"]["kind"] == "video"
    assert body["source"]["meta"]["frames_read"] == 3
    assert "[On-screen text - read from video frames]" in body["text"]
    assert "DIWALI OFFER" in body["text"]
    assert {"solicitation_cta", "inducement_offer", "guaranteed_outcome"} <= {f["rule_id"] for f in body["findings"]}


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not installed")
def test_voice_note_transcription_failure_is_reported(samples, monkeypatch):
    from app.extract import media
    from app.extract.transcribe import TranscriptionError

    def fail(*_):
        raise TranscriptionError("model not available")

    monkeypatch.setattr(media, "transcribe", fail)
    with open(samples / "voice_note_hi.ogg", "rb") as fh:
        r = client.post("/api/check", files={"file": ("WhatsApp Audio.ogg", fh, "audio/ogg")})
    assert r.status_code == 422  # audio without words -> the reason is shown
    assert "No words could be taken from this audio" in r.json()["detail"]
    assert "model not available" in r.json()["detail"]


def test_basic_auth(monkeypatch):
    monkeypatch.setenv("BASIC_AUTH_USER", "doctor")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "s3cret")
    assert client.get("/").status_code == 401
    token = base64.b64encode(b"doctor:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200
    assert client.get("/api/health").status_code == 200  # health stays open for uptime checks


def test_upload_limit(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    r = client.post("/api/check", files={"file": ("big.txt", b"a" * (1024 * 1024 + 10), "text/plain")})
    assert r.status_code == 413
