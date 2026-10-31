"""Text normalisation shared by extraction and rule matching.

All character replacements are one-to-one or removals done *before* analysis, so the offsets
returned by the checker always refer to the normalised text that is sent back to the UI.
"""

from __future__ import annotations

import re
import unicodedata

_CHAR_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        " ": " ",
        " ": " ",
        " ": " ",
        " ": " ",
        "–": "-",
        "—": "-",
        "−": "-",
        "％": "%",
    }
)
_DROP = re.compile("[​﻿­]")  # zero-width space, BOM, soft hyphen
_SPACES = re.compile(r"[ \t\f\v]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")


def normalize(text: str) -> str:
    """NFC-normalise, unify quotes/dashes/spaces and tidy whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _DROP.sub("", text.translate(_CHAR_MAP))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return _MANY_NEWLINES.sub("\n\n", text).strip()


def squash(text: str) -> str:
    """Lower-case, whitespace-collapsed form used for loose comparisons."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().lower()
