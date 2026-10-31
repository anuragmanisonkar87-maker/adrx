"""OCR helpers (Tesseract) for posters, scanned PDFs and video frames."""

from __future__ import annotations

import difflib
import io
import logging
import os
import re
import shutil
import string
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

AI_IMAGE_MAX_SIDE = 1568  # recommended max edge for Claude vision input


WINDOWS_TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
)


def _configure_tesseract() -> None:
    """Use TESSERACT_CMD if set; on Windows also find the standard install folder (it is often not on PATH)."""
    import pytesseract

    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if not cmd and os.name == "nt" and not shutil.which("tesseract"):
        cmd = next((p for p in WINDOWS_TESSERACT_PATHS if os.path.exists(p)), "")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd


@lru_cache(maxsize=1)
def available_languages() -> tuple[str, ...]:
    try:
        import pytesseract

        _configure_tesseract()
        return tuple(pytesseract.get_languages(config=""))
    except Exception:  # tesseract missing
        return ()


def ocr_ready() -> bool:
    return "eng" in available_languages()


def resolve_languages(requested: str) -> str:
    """Keep only the requested languages that are actually installed (falls back to eng)."""
    installed = set(available_languages())
    langs = [code for code in requested.split("+") if code in installed]
    return "+".join(langs) or "eng"


def _flatten(img: Image.Image) -> Image.Image:
    """Upright RGB or greyscale image; transparent areas become white."""
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, "white")
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert("RGB")
    return img


def _upscale(img: Image.Image, target_long_side: int) -> Image.Image:
    long_side = max(img.size)
    if long_side < target_long_side:
        scale = min(target_long_side / long_side, 3.0)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def _prepare(img: Image.Image, target_long_side: int = 1800) -> Image.Image:
    return ImageOps.grayscale(_upscale(_flatten(img), target_long_side))


def _clean_lines(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        # drop OCR noise: lines with too few letters/digits
        meaningful = sum(ch.isalnum() for ch in line)
        if meaningful >= 2 and meaningful >= 0.4 * len(line.replace(" ", "")):
            lines.append(line)
    return "\n".join(lines)


def ocr_document(img: Image.Image, languages: str) -> str:
    """OCR for posters, flyers and scanned pages."""
    import pytesseract

    gray = _prepare(img)
    langs = resolve_languages(languages)
    text = pytesseract.image_to_string(gray, lang=langs, config="--psm 3")
    if len(text.strip()) < 25:  # sparse layouts (posters) often read better with psm 11
        sparse = pytesseract.image_to_string(gray, lang=langs, config="--psm 11")
        if len(sparse.strip()) > len(text.strip()):
            text = sparse
    return _clean_lines(text)


# --- Video frames ----------------------------------------------------------------------------------
# Captions in video ads are usually white or coloured text with an outline or shadow on top of a moving
# picture, which Tesseract reads badly from a plain greyscale frame. So each frame is read in up to four
# "views" and the best reading of every text region is kept:
#   normal - the greyscale frame (dark text on a light background)
#   white  - only the near-white pixels, drawn black on white (white captions, text on dark banners)
#   colour - only the bright, saturated pixels (yellow, red, green captions)
#   bright - every bright pixel (outlined captions whose edges blend into the picture)
# A line counts only if it has enough real-looking words, which keeps OCR noise from photos out of the text.
# On a test set of 64 caption styles over photos this reads about 80% of the caption words (the plain
# greyscale reading found about 35%) and almost nothing on photos without text.

FRAME_SIDE = 1600
LINE_MIN_SCORE = 2.4  # about one 4-letter word read with 60% confidence
WORD_MIN_CONF = 30.0  # words read less surely than this are left out, even inside a line that counts
VIEW_MIN_SHARE, VIEW_MAX_SHARE = 0.002, 0.6  # skip a mask view that is (almost) empty or mostly background
_PUNCT = string.punctuation + "\u201c\u201d\u2018\u2019\u00ab\u00bb\u2014\u2013\u2022\u00b7\u2026\u00a6"


@dataclass
class _Line:
    words: list[str]
    score: float  # letters and digits read, weighted by confidence: the better reading of a region wins
    box: tuple[int, int, int, int]  # left, top, right, bottom


def _word_weight(word: str, conf: float) -> float:
    """How strongly a word points to real text: 0 for short tokens, symbols and typical OCR noise."""
    if conf < WORD_MIN_CONF:
        return 0.0
    core = word.strip(_PUNCT)
    if not core:
        return 0.0
    kinds = [unicodedata.category(ch)[0] for ch in core]
    letters = sum(k in "LM" for k in kinds)  # M: Devanagari vowel signs count with their letter
    digits = sum(k == "N" for k in kinds)
    if letters < 3 or letters + digits < 0.75 * len(core) or len(set(core.lower())) == 1:
        return 0.0
    if any("\u0900" <= ch <= "\u097f" for ch in core) and any(ch.isascii() and ch.isalnum() for ch in core):
        return 0.0  # Latin and Devanagari mixed in one word: OCR noise
    return letters * conf / 100


def _lines_from_data(data: dict) -> list[_Line]:
    """Text lines from Tesseract's word data that have enough real-looking words."""
    def conf(i: int) -> float:
        try:
            return float(data["conf"][i])
        except (TypeError, ValueError):
            return -1.0

    groups: dict[tuple[int, int, int], list[int]] = {}
    for i, word in enumerate(data["text"]):
        if word and word.strip():
            groups.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)
    lines = []
    for idx in groups.values():
        if sum(_word_weight(data["text"][i].strip(), conf(i)) for i in idx) < LINE_MIN_SCORE:
            continue  # no real-looking words: OCR noise
        kept = []
        for i in idx:
            alnum = sum(unicodedata.category(ch)[0] in "LMN" for ch in data["text"][i])
            if conf(i) >= WORD_MIN_CONF and alnum:  # pure symbols such as "|" or "~" are left out
                kept.append((i, alnum))
        box = (min(data["left"][i] for i, _ in kept), min(data["top"][i] for i, _ in kept),
               max(data["left"][i] + data["width"][i] for i, _ in kept),
               max(data["top"][i] + data["height"][i] for i, _ in kept))
        score = sum(alnum * conf(i) / 100 for i, alnum in kept)
        lines.append(_Line([data["text"][i].strip() for i, _ in kept], score, box))
    return lines


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Share of the smaller box that the other box covers."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return width * height / max(smaller, 1)


def _merge_lines(candidates: list[_Line]) -> list[str]:
    """Keep the best reading of each text region (the views overlap) and return lines in reading order."""
    chosen: list[_Line] = []
    for line in sorted(candidates, key=lambda c: -c.score):
        if all(_overlap(line.box, other.box) < 0.5 for other in chosen):
            chosen.append(line)
    rows: list[list[_Line]] = []
    for line in sorted(chosen, key=lambda c: c.box[1]):
        if rows:
            top, bottom = min(c.box[1] for c in rows[-1]), max(c.box[3] for c in rows[-1])
            shared = min(bottom, line.box[3]) - max(top, line.box[1])
            if shared >= 0.5 * min(bottom - top, line.box[3] - line.box[1]):  # same row: side by side
                rows[-1].append(line)
                continue
        rows.append([line])
    out = []
    for row in rows:
        text = _clean_lines(" ".join(w for line in sorted(row, key=lambda c: c.box[0]) for w in line.words))
        if text:
            out.append(text)
    return out


def _frame_views(img: Image.Image, side: int = FRAME_SIDE) -> list[Image.Image]:
    """The greyscale frame plus black-on-white masks of its white, its brightly coloured and its bright pixels."""
    import numpy as np

    base = _upscale(_flatten(img), side).convert("RGB")
    views = [ImageOps.grayscale(base)]
    pixels = np.asarray(base, dtype=np.int16)
    high, low = pixels.max(axis=2), pixels.min(axis=2)
    for mask in ((low > 170) & (high - low < 70),    # white or light grey
                 (high > 160) & (high - low > 100),  # bright, saturated colour
                 high > 200):                        # anything bright
        if VIEW_MIN_SHARE <= float(mask.mean()) <= VIEW_MAX_SHARE:
            views.append(Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)))
    return views


def ocr_frame(img: Image.Image, languages: str, side: int = FRAME_SIDE, workers: int = 4) -> list[str]:
    """OCR for video frames: reads dark, white and coloured text; returns the text lines in reading order.

    side: frames are enlarged to this long side first; workers: Tesseract processes run side by side.
    """
    import pytesseract

    langs = resolve_languages(languages)

    def read(view: Image.Image) -> list[_Line]:
        data = pytesseract.image_to_data(view, lang=langs, config="--psm 11", output_type=pytesseract.Output.DICT)
        return _lines_from_data(data)

    views = _frame_views(img, side)
    if len(views) == 1 or workers <= 1:
        return _merge_lines([line for view in views for line in read(view)])
    with ThreadPoolExecutor(max_workers=min(workers, len(views))) as pool:  # one Tesseract process per view
        return _merge_lines([line for lines in pool.map(read, views) for line in lines])


def dedupe_lines(lines: list[str], threshold: float = 0.85) -> list[str]:
    """Remove repeated on-screen text that appears across many frames."""
    kept: list[str] = []
    keys: list[str] = []
    for line in lines:
        key = re.sub(r"[^\w]+", " ", line.lower()).strip()
        if len(key) < 3:
            continue
        if any(key == k or difflib.SequenceMatcher(None, key, k).ratio() >= threshold for k in keys):
            continue
        kept.append(line)
        keys.append(key)
    return kept


def to_ai_jpeg(img: Image.Image, max_side: int = AI_IMAGE_MAX_SIDE, quality: int = 82) -> bytes:
    """Downscale and encode an image for the AI reviewer."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
