"""FastAPI app: web UI + JSON API for the NMC Ad Compliance Checker."""

from __future__ import annotations

import base64
import bisect
import hashlib
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__, extract
from app.config import Settings
from app.engine import ai_review
from app.engine.checker import NothingToCheck, run_check
from app.engine.rules import guideline_reference
from app.extract.ocr import available_languages
from app.textnorm import normalize

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _setup_logging() -> None:
    """Console logging plus a log file (default logs/adrx.log) that can be shared when something fails."""
    from logging.handlers import RotatingFileHandler

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_file = os.getenv("LOG_FILE", "logs/adrx.log").strip()
    if not log_file:
        return
    path = Path(log_file)
    path = path if path.is_absolute() else PROJECT_DIR / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
    except OSError:  # read-only disk etc.: console logging is enough
        pass


_setup_logging()
log = logging.getLogger("nmc")

STATIC_DIR = PROJECT_DIR / "static"
LANGUAGES = {"auto": None, "en": "en", "hi": "hi"}

app = FastAPI(
    title="NMC Ad Compliance Checker",
    version=__version__,
    description="Checks medical advertising content against the NMC Guidelines on Ethical Advertising (2026).",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional password protection: set BASIC_AUTH_USER and BASIC_AUTH_PASSWORD."""
    user, password = os.getenv("BASIC_AUTH_USER"), os.getenv("BASIC_AUTH_PASSWORD")
    if user and password and request.url.path != "/api/health":
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                given_user, _, given_pass = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(given_user, user) and secrets.compare_digest(given_pass, password)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="NMC checker"'})
    return await call_next(request)


API_DOC_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


@app.middleware("http")
async def public_site_pages(request: Request, call_next):
    """On the public website the developer API pages (/docs, /redoc) are hidden."""
    if request.url.path in API_DOC_PATHS and Settings.from_env().public_site:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)


class VisitorLimits:
    """Checks per visitor (IP address) per hour and per day, kept in memory (the app runs as one process)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._times: dict[str, list[float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._times.clear()

    def take(self, visitor: str, per_hour: int, per_day: int, now: float | None = None) -> int | None:
        """Counts one check. Returns None when it is allowed, else the seconds until the next one is."""
        now = time.time() if now is None else now
        with self._lock:
            times = self._times.setdefault(visitor, [])
            del times[:bisect.bisect_right(times, now - 86400)]  # forget checks older than a day
            for limit, window in ((per_hour, 3600), (per_day, 86400)):
                if limit > 0:
                    recent = times[bisect.bisect_right(times, now - window):]
                    if len(recent) >= limit:
                        return int(recent[len(recent) - limit] + window - now) + 1
            times.append(now)
            if len(self._times) > 20000:  # drop visitors with nothing left to count
                for key in [k for k, v in self._times.items() if not v or v[-1] <= now - 86400]:
                    del self._times[key]
            return None


visitor_limits = VisitorLimits()
_slots: dict[int, threading.BoundedSemaphore] = {}
_slots_lock = threading.Lock()


def _wait_text(seconds: int) -> str:
    if seconds < 90:
        return "in a minute"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"in about {minutes} minutes"
    return f"in about {round(minutes / 60)} hours"


def _count_visit(request: Request, settings: Settings) -> None:
    if settings.rate_limit_per_hour <= 0 and settings.rate_limit_per_day <= 0:
        return
    visitor = request.client.host if request.client else "unknown"
    wait = visitor_limits.take(visitor, settings.rate_limit_per_hour, settings.rate_limit_per_day)
    if wait is not None:
        log.info("Visitor limit reached for %s", visitor)
        raise HTTPException(429, f"You've reached the number of checks allowed for now. Please try again "
                                 f"{_wait_text(wait)}.", headers={"Retry-After": str(wait)})


@contextmanager
def _file_check_slot(settings: Settings):
    """Lets only MAX_PARALLEL_CHECKS file checks run at once; the others wait their turn for a while."""
    if settings.max_parallel_checks <= 0:
        yield
        return
    with _slots_lock:
        slot = _slots.setdefault(settings.max_parallel_checks, threading.BoundedSemaphore(settings.max_parallel_checks))
    if not slot.acquire(timeout=max(settings.queue_wait_seconds, 0)):
        raise HTTPException(503, "The website is busy checking other files. Please try again in a minute.",
                            headers={"Retry-After": "60"})
    try:
        yield
    finally:
        slot.release()


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    log.exception("Unexpected error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong while checking. Please try again."})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


_ai_status_cache: dict[str, tuple[float, dict]] = {}


def _ai_status(settings: Settings) -> dict:
    """Live key/model check (free), cached: 10 minutes when it works, 1 minute when it fails."""
    if not settings.ai_enabled:
        return ai_review.verify_access(settings)
    key = hashlib.sha256(
        f"{settings.anthropic_api_key}|{settings.anthropic_workspace_id}|{settings.claude_model}".encode()
    ).hexdigest()
    cached = _ai_status_cache.get(key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    status = ai_review.verify_access(settings)
    ttl = 600 if status["status"] == "ok" else 60
    _ai_status_cache.clear()
    _ai_status_cache[key] = (time.monotonic() + ttl, status)
    level = logging.INFO if status["status"] == "ok" else logging.WARNING
    log.log(level, "AI review status: %s", status["message"])
    return status


@app.get("/api/health")
def health(verify_ai: bool = False) -> dict:
    """Service status. With ?verify_ai=true it also checks the Anthropic key and model (free call)."""
    s = Settings.from_env()
    body = {
        "ok": True,
        "version": __version__,
        "ai_enabled": s.ai_enabled,
        "ai_model": s.claude_model if s.ai_enabled else None,
        "transcription": s.speech_backend,
        "ffmpeg": s.ffmpeg_available,
        "ocr_languages": list(available_languages()),
        "max_upload_mb": s.max_upload_mb,
        "max_media_minutes": round(s.max_media_seconds / 60, 1),
        "public": s.public_site,
        # where speech in audio/video is turned into text, when that is an outside service (for the privacy note)
        "speech_service": (urlparse(s.openai_base_url).hostname
                           if s.speech_backend == "openai" and s.openai_api_key else None),
        "guideline": guideline_reference(),
    }
    if verify_ai:
        status = _ai_status(s)
        body.update(ai_status=status["status"], ai_message=status["message"],
                    ai_model_name=status.get("model_name"))
    return body


def _save_upload(upload: UploadFile, settings: Settings) -> Path:
    limit = settings.max_upload_mb * 1024 * 1024
    suffix = Path(upload.filename or "").suffix.lower()[:10]
    size = 0
    with tempfile.NamedTemporaryFile(prefix="nmc-upload-", suffix=suffix, delete=False) as tmp:
        path = Path(tmp.name)
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                tmp.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB.")
            tmp.write(chunk)
    if size == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file is empty.")
    return path


@app.post("/api/check")
def check(
    request: Request,
    text: str | None = Form(None, description="Text to check, or the caption that goes with an uploaded file"),
    file: UploadFile | None = File(None, description="PDF, image, Word, text, audio or video file"),
    publisher: str = Form("rmp", description="rmp | hospital | agency | other"),
    channel: str = Form("social", description="social | youtube | website | whatsapp | tv_radio | print | hoarding | other"),
    ai_used: str = Form("no", description="no | yes | unsure"),
    language: str = Form("auto", description="Spoken language for audio/video: auto | en | hi"),
) -> dict:
    settings = Settings.from_env()
    started = time.monotonic()
    caption = (text or "").strip()
    filename = file.filename if file is not None and file.filename else None
    if filename or caption:
        _count_visit(request, settings)

    if filename:
        path = _save_upload(file, settings)
        try:
            with _file_check_slot(settings):
                extraction = extract.from_file(path, filename, settings, file.content_type,
                                               LANGUAGES.get(language, None))
        except extract.ExtractionError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            path.unlink(missing_ok=True)
        if caption:  # e.g. an Instagram post = image + caption
            caption_text = normalize(caption)[: settings.max_text_chars]
            body = extraction.text or "(no text found in the file)"
            extraction.text = f"[Post text / caption]\n{caption_text}\n\n[Text from the file]\n{body}"
            extraction.how.insert(0, "caption text")
    elif caption:
        extraction = extract.from_text(caption, settings)
    else:
        raise HTTPException(400, "Paste some text or upload a file to check.")

    try:
        result = run_check(extraction, publisher=publisher, channel=channel, ai_used=ai_used,
                           settings=settings, filename=filename)
    except NothingToCheck as exc:
        raise HTTPException(422, str(exc)) from exc
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result
