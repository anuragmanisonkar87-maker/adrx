"""Turn any supported input (text, PDF, image, Word, audio, video) into text the checker can read."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.textnorm import normalize

log = logging.getLogger(__name__)

EXTENSIONS = {
    "pdf": {".pdf"},
    "image": {".png", ".jpg", ".jpeg", ".jfif", ".pjpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".apng",
              ".avif", ".heic", ".heif"},
    "docx": {".docx"},
    "text": {".txt", ".md", ".csv", ".html", ".htm", ".srt", ".vtt", ".ass", ".ssa", ".sbv"},
    # Audio and video: anything ffmpeg can read is accepted, these are just the common names.
    "audio": {
        ".mp3", ".wav", ".wave", ".m4a", ".m4b", ".m4r", ".aac", ".adts", ".ogg", ".oga", ".opus", ".spx",
        ".flac", ".alac", ".amr", ".awb", ".3ga", ".wma", ".weba", ".aif", ".aiff", ".aifc", ".caf", ".au",
        ".snd", ".mka", ".mp2", ".mpa", ".mpga", ".ac3", ".eac3", ".ec3", ".dts", ".wv", ".ape", ".tta",
        ".ra", ".ram", ".gsm", ".voc", ".dss", ".ds2", ".qcp", ".w64", ".rf64", ".oma", ".aa", ".aax",
    },
    "video": {
        ".mp4", ".m4v", ".mov", ".qt", ".mkv", ".mk3d", ".webm", ".avi", ".divx", ".xvid", ".wmv", ".asf",
        ".flv", ".f4v", ".3gp", ".3gpp", ".3g2", ".mpeg", ".mpg", ".mpe", ".mpv", ".m1v", ".m2v", ".mp2v",
        ".ts", ".mts", ".m2ts", ".m2t", ".tp", ".trp", ".vob", ".vro", ".ogv", ".ogm", ".ogx", ".dv",
        ".mxf", ".rm", ".rmvb", ".nut", ".y4m", ".h264", ".264", ".h265", ".265", ".hevc", ".ivf", ".amv",
        ".mod", ".tod", ".mjpeg", ".mjpg", ".wtv", ".dvr-ms", ".gifv", ".av1", ".obu",
    },
}
MEDIA_KINDS = {"audio", "video"}


class ExtractionError(ValueError):
    """The file could not be read; the message is safe to show to the user."""


@dataclass
class Extraction:
    kind: str  # text | pdf | image | docx | audio | video
    text: str
    images: list[bytes] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    how: list[str] = field(default_factory=list)  # human-readable steps, e.g. "speech-to-text"


def _sniff(path: Path) -> str | None:
    with path.open("rb") as fh:  # read only the first bytes, not the whole (possibly large) file
        head = fh.read(16)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if head.startswith(b"PK\x03\x04"):
        return "docx"
    if head.startswith((b"OggS", b"ID3", b"fLaC", b"#!AMR", b"caff", b".snd", b"MAC ", b"wvpk", b"TTA1")) or (
        head[:4] == b"RIFF" and head[8:12] == b"WAVE") or (head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC")):
        return "audio"
    if head[4:8] == b"ftyp" or head.startswith((b"\x1aE\xdf\xa3", b"FLV", b".RMF", b"\x00\x00\x01\xba")) or (
        head[:4] == b"RIFF" and head[8:12] == b"AVI "):
        return "video"  # containers that can hold audio only: ffprobe decides audio vs video
    return None


def detect_kind(filename: str, path: Path, content_type: str | None = None,
                settings: Settings | None = None) -> str:
    suffix = Path(filename or "").suffix.lower()
    for kind, exts in EXTENSIONS.items():
        if suffix in exts:
            return kind
    if content_type:
        mime = content_type.split(";")[0].strip().lower()
        major = mime.split("/")[0]
        if mime == "application/pdf":
            return "pdf"
        if major in {"image", "audio", "video"}:
            return major
        if major == "text":
            return "text"
    kind = _sniff(path)
    if kind:
        return kind
    # Last resort for unusual names or no extension: can ffmpeg play it?
    if (settings or Settings.from_env()).ffmpeg_available:
        from app.extract import media

        if media.can_read(path):
            return "video"
    raise ExtractionError(
        "Unsupported file type. Upload text, PDF, Word (.docx), an image, or any audio or video file."
    )


def from_text(text: str, settings: Settings) -> Extraction:
    clean = normalize(text)
    warnings = []
    if len(clean) > settings.max_text_chars:
        clean = clean[: settings.max_text_chars]
        warnings.append(f"Text was long; only the first {settings.max_text_chars:,} characters were checked.")
    return Extraction(kind="text", text=clean, warnings=warnings, how=["pasted text"])


def from_file(path: Path, filename: str, settings: Settings, content_type: str | None = None,
              language: str | None = None) -> Extraction:
    from app.extract import documents, media

    kind = detect_kind(filename, path, content_type, settings)
    try:
        if kind == "pdf":
            doc = documents.extract_pdf(path, settings)
            how = ["PDF text"] + (["OCR of scanned pages"] if doc.meta.get("ocr_pages") else [])
            result = Extraction("pdf", doc.text, doc.images, doc.warnings, doc.meta, how)
        elif kind == "image" and _pillow_can_open(path):
            doc = documents.extract_image(path, settings)
            frames = doc.meta.get("frames", 1)
            how = [f"OCR of {doc.meta.get('frames_read', 1)} animation frames"] if frames > 1 else ["OCR of image text"]
            result = Extraction("image", doc.text, doc.images, doc.warnings, doc.meta, how)
        elif kind == "docx":
            doc = documents.extract_docx(path)
            result = Extraction("docx", doc.text, how=["Word document text"])
        elif kind == "text":
            doc = documents.extract_textfile(path)
            result = Extraction("text", doc.text, how=["text file"])
        else:  # audio, video, or an image format only ffmpeg can open (e.g. HEIC/AVIF)
            result = _from_media(path, settings, language)
    except ExtractionError:
        raise
    except Exception as exc:  # map library errors to a message that still says what went wrong
        from app.extract.media import MediaError

        if isinstance(exc, (MediaError, ValueError)):
            raise ExtractionError(str(exc)) from exc
        log.exception("Extraction failed for %s", filename)
        raise ExtractionError(
            f"Could not read this {kind} file ({type(exc).__name__}: {str(exc)[:200]})."
        ) from exc

    result.text = normalize(result.text)
    if len(result.text) > settings.max_text_chars:
        result.text = result.text[: settings.max_text_chars]
        result.warnings.append(f"Only the first {settings.max_text_chars:,} characters were checked.")
    return result


def _pillow_can_open(path: Path) -> bool:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def _from_media(path: Path, settings: Settings, language: str | None) -> Extraction:
    from app.extract import media

    if not settings.ffmpeg_available:
        raise ExtractionError("Audio and video need ffmpeg installed on the computer running the app.")
    info = media.probe(path)
    if not (info.has_audio or info.has_video):
        raise ExtractionError("This file has no audio or video that can be played.")
    still = info.has_video and not info.has_audio and info.duration < 0.5  # a single picture
    kind = "image" if still else ("video" if info.has_video else "audio")
    out = media.extract_media(path, settings, language, info)
    parts, how = [], []
    if out.transcript:
        how.append("speech-to-text")
        parts.append(("[Spoken words - auto-transcribed]\n" if kind == "video" else "") + out.transcript)
    if out.subtitle_lines:
        how.append("subtitles in the file")
        parts.append("[Subtitles in the file]\n" + "\n".join(out.subtitle_lines))
    if out.onscreen_lines:
        how.append("OCR of image text" if still else f"on-screen text (OCR of {out.frames_read} frames)")
        heading = "" if still else "[On-screen text - read from video frames]\n"
        parts.append(heading + "\n".join(out.onscreen_lines))
    if kind == "audio" and not out.transcript and not out.subtitle_lines:
        reason = " ".join(out.warnings) or "No speech was found."
        raise ExtractionError(f"No words could be taken from this audio. {reason}")
    meta = {"duration_s": round(info.duration, 1), "language": out.language, "frames_read": out.frames_read}
    return Extraction(kind, "\n\n".join(parts), out.images, out.warnings, meta, how)
