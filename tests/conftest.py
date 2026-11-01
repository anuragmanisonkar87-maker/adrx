import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"


@pytest.fixture(autouse=True)
def _no_real_ai(monkeypatch):
    """Tests never call the real Anthropic API unless a test injects its own client."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("AI_REVIEW_BASE_URL", raising=False)
    monkeypatch.setenv("AI_REVIEW_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("AI_REVIEW_EFFORT", "medium")
    monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)
    # website and speech settings: each test sets what it needs
    for name in ("PUBLIC_SITE", "AI_REVIEW", "RATE_LIMIT_PER_HOUR", "RATE_LIMIT_PER_DAY", "MAX_PARALLEL_CHECKS",
                 "QUEUE_WAIT_SECONDS", "OCR_FRAME_SIDE", "OCR_FRAME_WORKERS", "TRANSCRIBE_BACKEND", "OPENAI_API_KEY",
                 "OPENAI_BASE_URL", "OPENAI_TRANSCRIBE_MODEL", "WHISPER_CPU_THREADS", "WHISPER_BEAM_SIZE",
                 "KEEP_AWAKE", "KEEP_AWAKE_URL", "KEEP_AWAKE_MINUTES", "RENDER_EXTERNAL_URL"):
        monkeypatch.delenv(name, raising=False)
    from app.main import visitor_limits

    visitor_limits.reset()
    yield


@pytest.fixture
def samples() -> Path:
    return SAMPLES


def has_ffmpeg() -> bool:
    from shutil import which

    return which("ffmpeg") is not None and which("ffprobe") is not None


def has_tesseract() -> bool:
    from app.extract.ocr import ocr_ready

    return ocr_ready()


os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ["LOG_FILE"] = ""  # tests never write the app log file
