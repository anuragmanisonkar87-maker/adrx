"""Plain text from subtitle files and subtitle tracks (SRT, WebVTT, ASS/SSA, SBV)."""

from __future__ import annotations

import re

_TIMING = re.compile(r"-->|^\d{1,2}:\d{2}(:\d{2})?[.,]\d{2,3}\s*,\s*\d{1,2}:\d{2}")  # SRT/VTT and SBV timings
_TAGS = re.compile(r"<[^>]+>|\{\\[^}]*\}")
_HEADER = re.compile(r"^(WEBVTT|NOTE\b|STYLE\b|REGION\b|Kind:|Language:|\[Script Info\]|\[V4\+? Styles\]|"
                     r"\[Events\]|Format:|Style:|Title:|ScriptType:|PlayRes[XY]:|WrapStyle:|Collisions:)",
                     re.IGNORECASE)


def clean_subtitles(raw: str) -> list[str]:
    """Return the caption lines without numbering, timings, styling or repeats."""
    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = line.strip().lstrip("﻿")
        if not line or line.isdigit() or _TIMING.search(line) or _HEADER.match(line):
            continue
        if line.startswith("Dialogue:"):  # ASS/SSA: the text is the 10th comma-separated field
            parts = line.split(",", 9)
            line = parts[9] if len(parts) == 10 else ""
            line = line.replace("\\N", " ").replace("\\n", " ")
        line = re.sub(r"\s+", " ", _TAGS.sub("", line)).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return lines
