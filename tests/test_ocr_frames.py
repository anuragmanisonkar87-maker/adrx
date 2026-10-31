"""Reading on-screen text in video frames: white, coloured and banner captions over photos, without OCR noise."""

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.extract import ocr
from tests.conftest import ROOT, has_tesseract

PHOTOS = ROOT / "tests" / "data"


def data(*lines):
    """Tesseract word data (as image_to_data returns it) for (words, confs, (left, top), line_number) lines."""
    out = {k: [] for k in ("text", "conf", "block_num", "par_num", "line_num", "left", "top", "width", "height")}
    for words, confs, (left, top), line_no in lines:
        x = left
        for word, conf in zip(words.split(), confs):
            out["text"].append(word)
            out["conf"].append(conf)
            out["block_num"].append(line_no)
            out["par_num"].append(1)
            out["line_num"].append(1)
            out["left"].append(x)
            out["top"].append(top)
            out["width"].append(30 * len(word))
            out["height"].append(60)
            x += 30 * len(word) + 25
    return out


# --- scoring and merging (no Tesseract needed) -----------------------------------------------------

@pytest.mark.parametrize("word, conf, counts", [
    ("MUMBAI", 45, True), ("SURGEON", 90, True), ("इलाज", 80, True), ("GUARANTEED", 35, True),
    ("NO.1", 95, False), ("Dr.", 95, False), ("222", 90, False), ("2८222", 70, False), ("है", 85, False),
    ("lll", 90, False), ("|", 99, False), ("—", 99, False), ("MUMBAI", 20, False),
])
def test_word_weight(word, conf, counts):
    assert (ocr._word_weight(word, conf) > 0) is counts


def test_line_with_one_unsure_real_word_is_kept():
    # On some computers Tesseract is unsure of "MUMBAI" (conf 45) but sure of "NO.1": the line still counts
    lines = ocr._lines_from_data(data(("MUMBAI NO.1", [45, 70], (40, 100), 1), ("SURGEON", [93], (40, 220), 2)))
    assert ocr._merge_lines(lines) == ["MUMBAI NO.1", "SURGEON"]


def test_noise_lines_are_dropped():
    noise = data(("ae |", [70, 85], (10, 10), 1), ("2८222 है", [58, 87], (300, 400), 2), ("ve an", [68, 61], (900, 50), 3),
                 ("Wee", [39], (500, 600), 4), ("~ » \\", [80, 87, 89], (100, 650), 5))
    assert ocr._merge_lines(ocr._lines_from_data(noise)) == []


def test_best_reading_of_a_region_wins_and_rows_are_joined_left_to_right():
    gray = ocr._lines_from_data(data(("MUMBAINO", [66], (100, 300), 1)))
    white = ocr._lines_from_data(data(("MUMBAI NO.1", [92, 88], (100, 300), 1), ("SURGEON", [95], (520, 302), 2),
                                      ("100% GUARANTEED", [90, 91], (100, 450), 3)))
    assert ocr._merge_lines(gray + white) == ["MUMBAI NO.1 SURGEON", "100% GUARANTEED"]


def test_symbols_and_unsure_words_inside_a_real_line_are_left_out():
    lines = ocr._lines_from_data(data(("FREE | xq CONSULTATION ~", [91, 60, 12, 88, 75], (50, 50), 1)))
    assert ocr._merge_lines(lines) == ["FREE CONSULTATION"]


# --- real frames through Tesseract --------------------------------------------------------------------

needs_tesseract = pytest.mark.skipif(not has_tesseract(), reason="needs tesseract")


def _photo(name: str) -> Image.Image:
    with Image.open(PHOTOS / f"photo_{name}.jpg") as img:
        return img.convert("RGB").resize((1280, 720), Image.LANCZOS)


def _bold_font(size: int = 68):
    """Captions are usually bold: the app's own Mukta Bold, or Pillow's built-in font if .woff2 can't be read."""
    try:
        return ImageFont.truetype(str(ROOT / "static" / "fonts" / "mukta-latin-700-normal.woff2"), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _caption(img: Image.Image, text: str, style: str, font=None, height: float = 0.7) -> Image.Image:
    img = img.copy()
    draw = ImageDraw.Draw(img)
    font = font or _bold_font()
    stroke = 4 if style in ("outline", "hindi") else 0
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    x, y = (img.width - (right - left)) // 2, int(img.height * height)
    if style == "banner":
        draw.rectangle((0, y - 25, img.width, y + bottom + 30), fill=(140, 20, 30))
        draw.text((x, y), text, font=font, fill="white")
    elif style == "yellow":
        draw.text((x + 4, y + 4), text, font=font, fill="black")
        draw.text((x, y), text, font=font, fill=(255, 221, 0))
    else:
        draw.text((x, y), text, font=font, fill="white", stroke_width=stroke, stroke_fill="black")
    return img


def _found(lines: list[str], text: str) -> float:
    read = " ".join(lines).upper().split()
    words = text.upper().split()
    return sum(w in read for w in words) / len(words)


# Each of these is read completely by Tesseract 5.3 (fast language data) and 5.5 (standard and best language
# data), in either test font; the plain greyscale reading the app used before missed most of the outlined,
# yellow and Hindi ones. A word may still be misread on some computer, so two thirds of the words must be found.
@needs_tesseract
@pytest.mark.parametrize("style, text, photo, height", [
    ("outline", "PAINLESS LASER TREATMENT", "cat", 0.45),
    ("outline", "BOOK NOW LIMITED SLOTS", "rocket", 0.45),
    ("yellow", "100% GUARANTEED RESULTS", "rocket", 0.7),
    ("banner", "BOOK NOW LIMITED SLOTS", "cat", 0.7),
    ("banner", "MUMBAI NO.1 SURGEON", "rocket", 0.7),
])
def test_captions_over_photos_are_read(style, text, photo, height):
    lines = ocr.ocr_frame(_caption(_photo(photo), text, style, height=height), "eng+hin")
    assert _found(lines, text) >= 2 / 3, lines


@needs_tesseract
def test_hindi_caption_is_read():
    if "hin" not in ocr.available_languages():
        pytest.skip("Hindi OCR data is not installed")
    try:
        font = ImageFont.truetype(str(ROOT / "static" / "fonts" / "mukta-devanagari-700-normal.woff2"), 76)
    except OSError:
        pytest.skip("this Pillow build cannot open .woff2 fonts")
    text = "पक्का इलाज गारंटी"
    lines = ocr.ocr_frame(_caption(_photo("cat"), text, "hindi", font), "eng+hin")
    assert _found(lines, text) >= 2 / 3, lines


@needs_tesseract
@pytest.mark.parametrize("photo", ["coffee", "cat", "rocket"])
def test_photos_without_text_give_no_text(photo):
    # the old reading returned several junk "words" for photos like these; allow one stray word
    assert len(" ".join(ocr.ocr_frame(_photo(photo), "eng+hin")).split()) <= 1


@needs_tesseract
def test_plain_dark_text_on_white_reads_in_order():
    img = Image.new("RGB", (704, 576), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=70)
    draw.text((40, 150), "HAIR TRANSPLANT", fill="black", font=font)
    draw.text((40, 290), "SURGEON", fill="black", font=font)
    lines = ocr.ocr_frame(img, "eng+hin")
    assert _found(lines, "HAIR TRANSPLANT SURGEON") == 1 and lines[-1] == "SURGEON", lines
