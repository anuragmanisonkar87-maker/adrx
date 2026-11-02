"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"

try:  # load the project's .env file; its values win over variables already set in your shell
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=True)
except ImportError:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _path(name: str, default: Path) -> Path:
    value = (os.getenv(name) or "").strip()
    path = Path(value).expanduser() if value else default
    return path if path.is_absolute() else PROJECT_DIR / path


def _site_address() -> str | None:
    """The website's public address, for KEEP_AWAKE. Render sets RENDER_EXTERNAL_URL by itself."""
    value = (os.getenv("KEEP_AWAKE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if value and "://" not in value:
        value = f"https://{value}"
    return value or None


@dataclass(frozen=True)
class Settings:
    # AI review (Claude). Without a key the tool runs in "quick check" (rules-only) mode.
    anthropic_api_key: str | None = field(repr=False)
    anthropic_workspace_id: str | None  # only for keys that are not scoped to a workspace
    anthropic_base_url: str  # pinned so a stray ANTHROPIC_BASE_URL in the shell is never used
    claude_model: str
    claude_effort: str  # low | medium | high ; empty = model default
    ai_max_tokens: int
    ai_timeout_s: int
    ai_max_images: int

    # Speech-to-text for audio/video
    transcribe_backend: str  # local | openai | auto (openai when OPENAI_API_KEY is set, else local) | none
    whisper_model: str  # faster-whisper model size, e.g. small, medium, large-v3-turbo
    whisper_device: str  # cpu | cuda | auto
    whisper_compute_type: str  # int8 on CPU, float16 on GPU
    whisper_cpu_threads: int  # 0 = faster-whisper's default (4); 1 on a small server
    whisper_beam_size: int  # 5 = most accurate; 1 = several times faster
    openai_api_key: str | None = field(repr=False)  # for TRANSCRIBE_BACKEND=openai (OpenAI, Groq or any compatible API)
    openai_base_url: str
    openai_transcribe_model: str

    # OCR
    ocr_languages: str  # tesseract language codes, e.g. "eng+hin"
    ocr_frame_side: int  # video frames are enlarged to this long side before OCR (smaller = faster)
    ocr_frame_workers: int  # Tesseract processes per video frame at once (1 = least memory)

    # Limits
    max_upload_mb: int
    max_media_seconds: int
    max_pdf_pages: int
    max_text_chars: int
    max_video_frames: int

    # Public website (deploy/README.md)
    public_site: bool  # visitors' view: no setup hints, no API docs pages
    ai_review: bool  # AI_REVIEW=off keeps AI review off even if a key is set
    rate_limit_per_hour: int  # checks per visitor (IP address); 0 = no limit
    rate_limit_per_day: int
    max_parallel_checks: int  # file checks that run at the same time; 0 = no limit
    queue_wait_seconds: int  # how long a file check waits for a free slot before "busy"
    keep_awake: bool  # the app opens its own address every few minutes (free hosts sleep after 15 idle minutes)
    keep_awake_url: str | None  # the site's public address (KEEP_AWAKE_URL, else Render's RENDER_EXTERNAL_URL)
    keep_awake_minutes: int  # 1-14, so a visit always comes before Render's 15 idle minutes are up

    # Sign-in and user accounts (README "Sign-in and user accounts")
    require_login: bool  # REQUIRE_LOGIN=on: every page and every check needs an account
    database_url: str | None = field(repr=False)  # Supabase or other Postgres; empty = SQLite file in DATA_DIR
    data_dir: Path  # where the SQLite accounts file lives when DATABASE_URL is empty
    admin_username: str
    admin_password: str | None = field(repr=False)  # creates the first admin account when it doesn't exist
    admin_password_reset: bool  # ADMIN_PASSWORD_RESET=on: at start-up, set the admin's password to ADMIN_PASSWORD
    session_idle_hours: int  # signed out after this long without using the site
    session_max_days: int  # and after this long in any case

    @property
    def ai_enabled(self) -> bool:
        return self.ai_review and bool(self.anthropic_api_key)

    @property
    def speech_backend(self) -> str:
        """The speech-to-text backend actually used ("auto" picks the outside API only when its key is set)."""
        if self.transcribe_backend == "auto":
            return "openai" if self.openai_api_key else "local"
        return self.transcribe_backend

    @property
    def ffmpeg_available(self) -> bool:
        return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            anthropic_api_key=(os.getenv("ANTHROPIC_API_KEY") or "").strip() or None,
            anthropic_workspace_id=(os.getenv("ANTHROPIC_WORKSPACE_ID") or "").strip() or None,
            anthropic_base_url=(os.getenv("AI_REVIEW_BASE_URL") or "https://api.anthropic.com").strip(),
            # app-specific names, so other Claude tools' variables (e.g. CLAUDE_EFFORT) never leak in
            claude_model=(os.getenv("AI_REVIEW_MODEL") or "claude-sonnet-5-5").strip(),
            claude_effort=(os.getenv("AI_REVIEW_EFFORT") or "medium").strip().lower(),
            ai_max_tokens=_int("AI_MAX_TOKENS", 16000),
            ai_timeout_s=_int("AI_TIMEOUT_SECONDS", 180),
            ai_max_images=_int("AI_MAX_IMAGES", 6),
            transcribe_backend=os.getenv("TRANSCRIBE_BACKEND", "local").strip().lower(),
            whisper_model=os.getenv("WHISPER_MODEL", "small"),
            whisper_device=os.getenv("WHISPER_DEVICE", "cpu"),
            whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
            whisper_cpu_threads=max(_int("WHISPER_CPU_THREADS", 0), 0),
            whisper_beam_size=max(_int("WHISPER_BEAM_SIZE", 5), 1),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_transcribe_model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1"),
            ocr_languages=os.getenv("OCR_LANGUAGES", "eng+hin"),
            ocr_frame_side=max(_int("OCR_FRAME_SIDE", 1600), 640),
            ocr_frame_workers=max(_int("OCR_FRAME_WORKERS", 4), 1),
            max_upload_mb=_int("MAX_UPLOAD_MB", 500),
            max_media_seconds=_int("MAX_MEDIA_SECONDS", 600),
            max_pdf_pages=_int("MAX_PDF_PAGES", 30),
            max_text_chars=_int("MAX_TEXT_CHARS", 30000),
            max_video_frames=_int("MAX_VIDEO_FRAMES", 24),
            public_site=_bool("PUBLIC_SITE", False),
            ai_review=_bool("AI_REVIEW", True),
            rate_limit_per_hour=_int("RATE_LIMIT_PER_HOUR", 0),
            rate_limit_per_day=_int("RATE_LIMIT_PER_DAY", 0),
            max_parallel_checks=_int("MAX_PARALLEL_CHECKS", 0),
            queue_wait_seconds=_int("QUEUE_WAIT_SECONDS", 120),
            keep_awake=_bool("KEEP_AWAKE", False),
            keep_awake_url=_site_address(),
            keep_awake_minutes=min(max(_int("KEEP_AWAKE_MINUTES", 10), 1), 14),
            require_login=_bool("REQUIRE_LOGIN", False),
            database_url=(os.getenv("DATABASE_URL") or "").strip() or None,
            data_dir=_path("DATA_DIR", PROJECT_DIR / "data"),
            admin_username=(os.getenv("ADMIN_USERNAME") or "admin").strip().lower(),
            admin_password=os.getenv("ADMIN_PASSWORD") or None,
            admin_password_reset=_bool("ADMIN_PASSWORD_RESET", False),
            session_idle_hours=min(max(_int("SESSION_IDLE_HOURS", 12), 1), 24 * 30),
            session_max_days=min(max(_int("SESSION_MAX_DAYS", 7), 1), 90),
        )
