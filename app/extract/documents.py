"""Text extraction for PDFs, images, Word files and plain-text files."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from app.config import Settings
from app.extract import ocr


@dataclass
class DocText:
    text: str
    images: list[bytes] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def extract_pdf(path: Path, settings: Settings) -> DocText:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:  # encrypted or corrupt
        raise ValueError(f"Could not open the PDF ({exc})") from exc
    warnings: list[str] = []
    pages: list[str] = []
    images: list[bytes] = []
    ocr_pages = 0
    try:  # always close: Windows cannot delete the temp file while it is open
        total = len(pdf)
        if total > settings.max_pdf_pages:
            warnings.append(f"Only the first {settings.max_pdf_pages} of {total} pages were checked.")
        can_ocr = ocr.ocr_ready()
        for index in range(min(total, settings.max_pdf_pages)):
            page = pdf[index]
            text = (page.get_textpage().get_text_range() or "").strip()
            needs_ocr = len(text) < 25
            if needs_ocr or len(images) < settings.ai_max_images:
                bitmap = page.render(scale=2.8 if needs_ocr else 1.6)
                rendered = bitmap.to_pil()
                if needs_ocr and can_ocr:
                    text = ocr.ocr_document(rendered, settings.ocr_languages)
                    ocr_pages += 1
                if len(images) < settings.ai_max_images:
                    images.append(ocr.to_ai_jpeg(rendered))
            pages.append(text)
    finally:
        pdf.close()
    if ocr_pages and not can_ocr:
        warnings.append("Scanned pages could not be read: Tesseract OCR is not installed.")
    if total > 1:
        body = "\n\n".join(f"[Page {i + 1}]\n{t}" for i, t in enumerate(pages) if t)
    else:
        body = pages[0] if pages else ""
    return DocText(text=body, images=images, warnings=warnings, meta={"pages": total, "ocr_pages": ocr_pages})


def _image_frames(raw: Image.Image, max_frames: int) -> list[Image.Image]:
    """All frames worth reading: one for a still image, an even sample for animated GIF/WebP/APNG."""
    count = getattr(raw, "n_frames", 1) or 1
    if count <= 1:
        raw.seek(0)
        return [raw.convert("RGB")]
    take = min(count, max(max_frames, 1))
    indexes = sorted({round(i * (count - 1) / max(take - 1, 1)) for i in range(take)})
    frames = []
    for index in indexes:
        raw.seek(index)
        frames.append(raw.convert("RGB"))
    return frames


def extract_image(path: Path, settings: Settings) -> DocText:
    from app.extract.media import _differs, _frame_signature, _pick_evenly

    with Image.open(path) as raw:
        frames = _image_frames(raw, settings.max_video_frames)
    distinct, last = [], None
    for frame in frames:  # skip frames that look the same as the previous one
        sig = _frame_signature(frame)
        if last is None or _differs(sig, last):
            distinct.append(frame)
            last = sig
    warnings: list[str] = []
    text = ""
    if ocr.ocr_ready():
        if len(distinct) == 1:
            text = ocr.ocr_document(distinct[0], settings.ocr_languages)
        else:
            lines: list[str] = []
            for frame in distinct:
                lines.extend(ocr.ocr_document(frame, settings.ocr_languages).splitlines())
            text = "\n".join(ocr.dedupe_lines(lines))
    else:
        warnings.append("Text in the image was not read: Tesseract OCR is not installed.")
    first = distinct[0]
    return DocText(text=text, images=[ocr.to_ai_jpeg(f) for f in _pick_evenly(distinct, settings.ai_max_images)],
                   warnings=warnings,
                   meta={"width": first.width, "height": first.height, "frames": len(frames),
                         "frames_read": len(distinct)})


def extract_docx(path: Path) -> DocText:
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(dict.fromkeys(cells)))
    return DocText(text="\n".join(parts))


_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa", ".sbv"}


def extract_textfile(path: Path) -> DocText:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", "replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", "replace")
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        text = html.unescape(_TAGS.sub(" ", text))
    elif suffix in SUBTITLE_EXTENSIONS:
        from app.extract.subtitles import clean_subtitles

        text = "\n".join(clean_subtitles(text))
    return DocText(text=text)
