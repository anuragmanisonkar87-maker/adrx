"""Deterministic rule engine: finds red-flag phrases and maps them to NMC guideline clauses."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
RULES_FILE = KNOWLEDGE_DIR / "rules.yaml"

# "results are NOT guaranteed", "there is no miracle cure", "we cannot cure diabetes" ...
_NEG_BEFORE = re.compile(
    r"(?:\bno|\bnot|\bnever|\bnor|\bwithout|\bcannot|n't)\s+(?:[\w'-]+\s+){0,2}$", re.IGNORECASE
)
# Hindi negation placed after the phrase: "गारंटी नहीं"
_NEG_AFTER = re.compile(r"^\s*(?:नहीं|नही|नहीँ)")
_WINDOW = 40


def _compile(pattern: str) -> re.Pattern:
    return re.compile(unicodedata.normalize("NFC", pattern), re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str
    clauses: tuple[str, ...]
    why: str
    fix: str
    serious: bool
    negation: bool
    patterns: tuple[re.Pattern, ...]
    exclude_before: re.Pattern | None
    exclude_after: re.Pattern | None


@dataclass(frozen=True)
class RuleHit:
    rule: Rule
    start: int
    end: int


@lru_cache(maxsize=4)
def load_rules(path: str | None = None) -> tuple[Rule, ...]:
    data = yaml.safe_load(Path(path or RULES_FILE).read_text(encoding="utf-8"))
    rules = []
    for raw in data["rules"]:
        rules.append(
            Rule(
                id=raw["id"],
                title=raw["title"],
                severity=raw.get("severity", "medium"),
                clauses=tuple(raw.get("clauses", [])),
                why=" ".join(raw.get("why", "").split()),
                fix=" ".join(raw.get("fix", "").split()),
                serious=bool(raw.get("serious", False)),
                negation=bool(raw.get("negation", False)),
                patterns=tuple(_compile(p) for p in raw.get("patterns", [])),
                exclude_before=_compile(raw["exclude_before"]) if raw.get("exclude_before") else None,
                exclude_after=_compile(raw["exclude_after"]) if raw.get("exclude_after") else None,
            )
        )
    return tuple(rules)


def guideline_reference(path: str | None = None) -> str:
    data = yaml.safe_load(Path(path or RULES_FILE).read_text(encoding="utf-8"))
    return data.get("guideline", "")


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def scan(text: str, rules: tuple[Rule, ...] | None = None) -> list[RuleHit]:
    """Return every rule match in ``text`` (already normalised), merged per rule."""
    rules = rules or load_rules()
    hits: list[RuleHit] = []
    for rule in rules:
        spans: list[tuple[int, int]] = []
        for pattern in rule.patterns:
            for match in pattern.finditer(text):
                start, end = _trim(text, *match.span())
                if end <= start:
                    continue
                before = text[max(0, start - _WINDOW) : start]
                after = text[end : end + _WINDOW]
                if rule.exclude_before and rule.exclude_before.search(before):
                    continue
                if rule.exclude_after and rule.exclude_after.search(after):
                    continue
                if rule.negation and (_NEG_BEFORE.search(before) or _NEG_AFTER.search(after)):
                    continue
                spans.append((start, end))
        spans.sort()
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        hits.extend(RuleHit(rule, s, e) for s, e in merged)
    hits.sort(key=lambda h: (h.start, h.end))
    return hits
