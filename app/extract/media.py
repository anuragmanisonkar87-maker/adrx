"""Audio and video extraction with ffmpeg: speech transcript + on-screen text from frames."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

from app.config import Settings
from app.extract import ocr
from app.extract.transcribe import TranscriptionError, transcribe

log = logging.getLogger(__name__)


class MediaError(RuntimeError):
    pass


# "Video" codecs ffmpeg uses for ANSI/text art; random non-media files are often probed as these.
TEXT_ART_CODECS = {"bintext", "xbin", "idf", "ansi", "tty"}

# Text subtitle codecs ffmpeg can turn into SRT (bitmap subtitles such as DVD/Blu-ray need OCR and are skipped)
TEXT_SUBTITLE_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "microdvd", "subviewer", "subviewer1",
    "jacosub", "sami", "realtext", "pjs", "mpl2", "vplayer", "stl",
}


@dataclass
class MediaInfo:
    duration: float
    has_audio: bool
    has_video: bool
    subtitle_streams: list[int] = field(default_factory=list)


@dataclass
class MediaText:
    transcript: str = ""
    language: str | None = None
    onscreen_lines: list[str] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    frames_read: int = 0
    subtitle_lines: list[str] = field(default_factory=list)


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)
    except FileNotFoundError as exc:
        raise MediaError("ffmpeg/ffprobe is not installed on the server") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Media processing timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "ignore").strip().splitlines()[-1:] or ["unknown error"]
        raise MediaError(f"Could not read the media file ({detail[0][:200]})") from exc


def probe(path: Path) -> MediaInfo:
    out = _run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=60,
    )
    info = json.loads(out.stdout or b"{}")
    streams = info.get("streams", [])
    has_video = any(
        s.get("codec_type") == "video"
        and not s.get("disposition", {}).get("attached_pic")  # album art in music files
        and s.get("codec_name") not in TEXT_ART_CODECS  # ffmpeg reads random files as "ANSI art" video
        for s in streams
    )
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    subtitles = [int(s["index"]) for s in streams
                 if s.get("codec_type") == "subtitle" and s.get("codec_name") in TEXT_SUBTITLE_CODECS]
    try:
        duration = float(info.get("format", {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    if not streams:
        raise MediaError("No audio or video streams found in the file")
    return MediaInfo(duration=duration, has_audio=has_audio, has_video=has_video, subtitle_streams=subtitles)


def can_read(path: Path) -> bool:
    """True if ffmpeg recognises the file as audio or video (used for unknown file extensions)."""
    try:
        info = probe(path)
    except (MediaError, ValueError):
        return False
    return info.has_audio or info.has_video


def _extract_audio(path: Path, workdir: Path, max_seconds: int) -> Path:
    wav = workdir / "audio16k.wav"
    _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-vn", "-sn", "-dn", "-ac", "1",
          "-ar", "16000", "-c:a", "pcm_s16le", "-t", str(max_seconds), "-f", "wav", str(wav)])
    return wav


def _extract_subtitles(path: Path, workdir: Path, streams: list[int], max_tracks: int = 2) -> list[str]:
    from app.extract.subtitles import clean_subtitles

    lines: list[str] = []
    for index in streams[:max_tracks]:
        target = workdir / f"subtitles_{index}.srt"
        try:
            _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", f"0:{index}",
                  "-f", "srt", str(target)], timeout=120)
        except MediaError as exc:
            log.warning("Subtitle track %s could not be read: %s", index, exc)
            continue
        lines.extend(clean_subtitles(target.read_text(encoding="utf-8", errors="replace")))
    return lines


def _extract_frames(path: Path, workdir: Path, duration: float, settings: Settings) -> list[Path]:
    seconds = min(duration or settings.max_media_seconds, settings.max_media_seconds)
    interval = max(2.0, seconds / max(settings.max_video_frames, 1))
    pattern = workdir / "frame_%03d.jpg"
    # No seeking: in files with few keyframes (MPEG-TS from phones/TV) a seek can land where nothing decodes.
    _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-an", "-sn", "-dn",
          "-t", str(settings.max_media_seconds),
          "-vf", f"fps=1/{interval:.3f},scale='min(1280,iw)':-2",
          "-frames:v", str(settings.max_video_frames), "-q:v", "3", str(pattern)])
    return sorted(workdir.glob("frame_*.jpg"))


def _frame_signature(img: Image.Image) -> Image.Image:
    return ImageOps.grayscale(img).resize((192, 108))


def _differs(a: Image.Image, b: Image.Image, pixel_delta: int = 40, min_changed: float = 0.004) -> bool:
    """True when enough pixels changed clearly - sensitive to new captions on an unchanged background,
    but not to compression noise."""
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > pixel_delta else 0)
    changed = ImageStat.Stat(diff).mean[0] / 255
    return changed > min_changed


def _pick_evenly(items: list, count: int) -> list:
    if len(items) <= count:
        return items
    step = (len(items) - 1) / (count - 1) if count > 1 else 0
    return [items[round(i * step)] for i in range(count)]


def extract_media(path: Path, settings: Settings, language: str | None, info: MediaInfo | None = None) -> MediaText:
    info = info or probe(path)
    result = MediaText()
    if info.duration > settings.max_media_seconds:
        result.warnings.append(
            f"Only the first {settings.max_media_seconds // 60} min of {info.duration / 60:.1f} min were checked."
        )
    # ignore_cleanup_errors: on Windows a file another library still holds open cannot be deleted
    with tempfile.TemporaryDirectory(prefix="nmc-media-", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        if info.has_audio:
            wav = _extract_audio(path, workdir, settings.max_media_seconds)
            try:
                transcript = transcribe(wav, language, settings)
                result.transcript, result.language = transcript.text, transcript.language
                if not transcript.text:
                    result.warnings.append("No speech was detected in the audio.")
            except TranscriptionError as exc:
                if settings.public_site:  # visitors don't need server set-up details; the log keeps them
                    log.warning("Speech could not be transcribed: %s", exc)
                    result.warnings.append("Speech could not be turned into text on this website right now.")
                else:
                    result.warnings.append(f"Speech could not be transcribed: {exc}")
        elif not info.has_video:
            raise MediaError("The file has no audio track")

        if info.subtitle_streams:
            result.subtitle_lines = _extract_subtitles(path, workdir, info.subtitle_streams)

        if info.has_video:
            frames = _extract_frames(path, workdir, info.duration, settings)
            distinct: list[Image.Image] = []
            last_sig = None
            lines: list[str] = []
            can_ocr = ocr.ocr_ready()
            if not can_ocr:
                result.warnings.append("On-screen text was not read: Tesseract OCR is not installed.")
            for frame_path in frames:
                with Image.open(frame_path) as raw:
                    img = raw.convert("RGB")
                sig = _frame_signature(img)
                if last_sig is not None and not _differs(sig, last_sig):
                    continue  # same scene as the previous frame: skip OCR
                last_sig = sig
                distinct.append(img)
                if can_ocr:
                    lines.extend(ocr.ocr_frame(img, settings.ocr_languages, settings.ocr_frame_side,
                                               settings.ocr_frame_workers))
                    result.frames_read += 1
            result.onscreen_lines = ocr.dedupe_lines(lines)
            result.images = [ocr.to_ai_jpeg(img) for img in _pick_evenly(distinct, settings.ai_max_images)]
    return result
