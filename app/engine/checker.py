"""Orchestrates a compliance check: rules -> (optional) AI review -> merged findings, verdict, highlights."""

from __future__ import annotations

import logging
import re
import time

from app.config import Settings
from app.engine import ai_review, disclosures
from app.engine import rules as rule_engine
from app.extract import Extraction
from app.textnorm import normalize, squash

log = logging.getLogger(__name__)

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
RANK_SEVERITY = {v: k for k, v in SEVERITY_RANK.items()}

PUBLISHERS = {"rmp", "hospital", "agency", "other"}
CHANNELS = {"social", "youtube", "website", "whatsapp", "tv_radio", "print", "hoarding", "other"}
AI_USED = {"no", "yes", "unsure"}

DISCLAIMER = (
    "This tool helps you check content against the NMC Guidelines on Ethical Advertising and Public "
    "Communication (06-10-2026). It is guidance, not legal advice; responsibility for published content "
    "stays with the doctor or institution (clause 4.4)."
)
PENALTY_LADDER = [
    "1st violation: warning and mandatory ethics training",
    "2nd violation: censure and monetary penalty",
    "3rd violation: suspension of registration for 3-6 months",
    "Serious violation (misleading cure claims, inducements, digital mass solicitation): suspension for 6-12 months",
    "Repeated violations: removal from the medical register for 1-3 years",
]


class NothingToCheck(ValueError):
    pass


def _rule_items(text: str) -> list[dict]:
    """One item per (rule, distinct phrase). These are also the pre-flags sent to the AI reviewer."""
    groups: dict[tuple[str, str], dict] = {}
    for hit in rule_engine.scan(text):
        quote = text[hit.start : hit.end]
        key = (hit.rule.id, squash(quote))
        if key not in groups:
            groups[key] = {
                "id": "",
                "rule_id": hit.rule.id,
                "quote": quote,
                "spans": [],
                "severity": hit.rule.severity,
                "title": hit.rule.title,
                "clauses": list(hit.rule.clauses),
                "why": hit.rule.why,
                "fix": hit.rule.fix,
                "source": "rules",
                "location": "text",
                "serious": hit.rule.serious,
            }
        groups[key]["spans"].append([hit.start, hit.end])
    items = list(groups.values())
    for i, item in enumerate(items, 1):
        item["id"] = f"R{i}"
    return items


def _group_by_rule(items: list[dict]) -> list[dict]:
    """Collapse rule items into one finding per rule, listing every phrase it matched."""
    groups: dict[str, dict] = {}
    for item in items:
        group = groups.get(item["rule_id"])
        if group is None:
            group = {k: v for k, v in item.items() if k not in {"quote", "spans", "ai_note"}}
            group.update(quotes=[], spans=[], ai_notes=[])
            groups[item["rule_id"]] = group
        group["quotes"].append({"text": item["quote"], "spans": sorted(item["spans"])})
        group["spans"].extend(item["spans"])
        if item.get("ai_note"):
            group["ai_notes"].append(item["ai_note"])
    out = []
    for group in groups.values():
        group["spans"].sort()
        group["quote"] = group["quotes"][0]["text"]
        group["ai_note"] = " ".join(group.pop("ai_notes")) or None
        out.append(group)
    return out


def _context_findings(ai_used: str, channel: str) -> list[dict]:
    out = []
    base = {"quote": "", "quotes": [], "spans": [], "source": "rules", "location": "context", "serious": False,
            "ai_note": None}
    if ai_used == "yes":
        out.append(dict(base, rule_id="ai_generated", severity="medium", clauses=["7.2"],
                        title="AI-generated promotional content",
                        why="AI-generated promotional campaigns for commercial interest are prohibited. AI content "
                            "is acceptable only if it is otherwise compliant, carries a clear 'AI-generated' source "
                            "mark, and does not create or alter patient images, voices, testimonials or endorsements.",
                        fix="Use AI only for factual or educational material, add a visible 'AI-generated' label, "
                            "and never use AI-made patients, voices or endorsements."))
    elif ai_used == "unsure":
        out.append(dict(base, rule_id="ai_maybe", severity="low", clauses=["7.2"],
                        title="Check whether any part was made with AI",
                        why="If any image, voice, avatar or text was created or materially altered with AI, it must "
                            "carry a source mark saying so.",
                        fix="Add an 'AI-generated' label to any AI-made part, or confirm none was used."))
    if channel == "hoarding":
        out.append(dict(base, rule_id="hoarding", severity="low", clauses=["5.2"],
                        title="Hoardings may carry only factual information",
                        why="Public hoardings/billboards may be used only for factual information such as "
                            "infrastructure and facilities; any unverified claim is treated as commercial and prohibited.",
                        fix="Limit the hoarding to name, facilities, services, address and contact details."))
    return out


def locate(text: str, quote: str) -> list[list[int]]:
    """Find every occurrence of an AI quote in the text (exact, then whitespace/case-insensitive)."""
    quote = normalize(quote).strip(" \"'.,;:!?")
    if not quote:
        return []
    spans = []
    start = text.find(quote)
    while start != -1:
        spans.append([start, start + len(quote)])
        start = text.find(quote, start + len(quote))
    if spans:
        return spans
    tokens = quote.split()
    if not tokens:
        return []
    pattern = re.compile(r"\s+".join(re.escape(tok) for tok in tokens), re.IGNORECASE)
    return [[m.start(), m.end()] for m in pattern.finditer(text)]


def _overlap(a: list[list[int]], b: list[list[int]]) -> int:
    return sum(max(0, min(e1, e2) - max(s1, s2)) for s1, e1 in a for s2, e2 in b)


_SERIOUS_HINT = re.compile(r"\b(?:cure|cures|cured|guarantee\w*|inducement|discount|offer|free)\b", re.IGNORECASE)


def _ai_finding(text: str, item: dict) -> dict:
    severity = str(item.get("severity", "medium")).lower()
    severity = severity if severity in SEVERITY_RANK else "medium"
    clause = str(item.get("clause", "")).strip()
    quote = str(item.get("quote", "")).strip()
    visual = str(item.get("location", "text")).lower() == "visual"
    spans = [] if visual or not quote else locate(text, quote)
    shown = text[spans[0][0] : spans[0][1]] if spans else quote
    title = item.get("title") or "Possible conflict"
    return {
        "id": "",
        "rule_id": None,
        "quote": shown,
        "quotes": [{"text": shown, "spans": spans}] if shown else [],
        "spans": spans,
        "severity": severity,
        "title": title,
        "clauses": [clause] if clause else [],
        "why": item.get("why", ""),
        "fix": item.get("fix", ""),
        "source": "ai",
        "location": "visual" if visual else "text",
        "serious": severity == "high" and bool(_SERIOUS_HINT.search(f"{title} {clause}"))
        and ("8.1(x)" in clause or "8.1(iii)" in clause or "8.1(vi)" in clause),
        "ai_note": None,
    }


def _merge_ai(text: str, items: list[dict], data: dict) -> tuple[list[dict], list[dict]]:
    """Combine rule items with the AI review.

    - pre-flags the AI marked invalid are dismissed (shown separately for transparency);
    - AI findings become the main cards; valid rule items they cover add their clauses as evidence;
    - valid rule items the AI did not cover stay as rule cards (grouped per rule).
    """
    reviews = {r.get("flag_id"): r for r in data.get("preflag_reviews", [])}
    valid, dismissed = [], []
    for item in items:
        review = reviews.get(item["id"])
        if review is not None and review.get("valid") is False:
            dismissed.append({"quote": item["quote"], "title": item["title"], "note": review.get("note", "")})
            continue
        if review and review.get("note"):
            item["ai_note"] = review["note"]
        valid.append(item)

    ai_findings = [_ai_finding(text, raw) for raw in data.get("findings", [])]
    anchored = [f for f in ai_findings if f["spans"]]
    leftovers = []
    for item in valid:
        covering = [f for f in anchored if _overlap(item["spans"], f["spans"])]
        if not covering:
            leftovers.append(item)
            continue
        best = max(covering, key=lambda f: _overlap(item["spans"], f["spans"]))
        best["source"] = "rules+ai"
        best["severity"] = RANK_SEVERITY[max(SEVERITY_RANK[best["severity"]], SEVERITY_RANK[item["severity"]])]
        best["serious"] = best["serious"] or item["serious"]
        for clause in item["clauses"]:
            if clause not in best["clauses"]:
                best["clauses"].append(clause)
        uncovered = [s for s in item["spans"] if not any(_overlap([s], f["spans"]) for f in covering)]
        if uncovered:
            leftovers.append(dict(item, spans=uncovered))
    return ai_findings + _group_by_rule(leftovers), dismissed


def _order_and_number(findings: list[dict]) -> list[dict]:
    def key(f: dict):
        location_rank = {"text": 0, "visual": 1, "context": 2}[f["location"]]
        first = f["spans"][0][0] if f["spans"] else 10**9
        return (location_rank, first, -SEVERITY_RANK[f["severity"]])

    ordered = sorted(findings, key=key)
    for i, finding in enumerate(ordered, 1):
        finding["id"] = f"F{i}"
    return ordered


def build_highlights(findings: list[dict]) -> list[dict]:
    intervals = [(s, e, f["id"], SEVERITY_RANK[f["severity"]]) for f in findings for s, e in f["spans"] if e > s]
    points = sorted({p for s, e, *_ in intervals for p in (s, e)})
    out: list[dict] = []
    for a, b in zip(points, points[1:]):
        covering = [iv for iv in intervals if iv[0] <= a and iv[1] >= b]
        if not covering:
            continue
        ids = sorted({iv[2] for iv in covering}, key=lambda x: int(x[1:]))
        severity = RANK_SEVERITY[max(iv[3] for iv in covering)]
        if out and out[-1]["end"] == a and out[-1]["finding_ids"] == ids:
            out[-1]["end"] = b
        else:
            out.append({"start": a, "end": b, "severity": severity, "finding_ids": ids})
    return out


def _verdict(findings: list[dict], missing: list[dict]) -> str:
    severities = {f["severity"] for f in findings}
    if "high" in severities:
        return "non_compliant"
    if "medium" in severities or missing:
        return "needs_changes"
    return "compliant"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _phrase_counts(findings: list[dict]) -> dict:
    """Problems per severity, counted in highlighted phrases (a finding without a phrase counts once)."""
    counts = {}
    for severity in ("high", "medium", "low"):
        group = [f for f in findings if f["severity"] == severity]
        phrases = {tuple(s) for f in group for s in f["spans"]}
        counts[severity] = len(phrases) + sum(1 for f in group if not f["spans"])
    return counts


def _headline(verdict: str, counts: dict) -> str:
    if verdict == "compliant":
        return "No conflicts with the NMC advertising guidelines"
    if verdict == "needs_changes":
        return _plural(counts["medium"] + counts["disclosures"], "thing", "things") + " to fix before posting"
    total = counts["high"] + counts["medium"]
    return _plural(total, "phrase breaks", "phrases break") + " the NMC advertising guidelines"


def _summary(verdict: str, findings: list[dict], ai_data: dict | None) -> str:
    if ai_data and ai_data.get("verdict_reason"):
        return ai_data["verdict_reason"]
    if verdict == "compliant":
        return ("The phrase check found no guarantees, superlatives, offers, testimonials or other claims "
                "that the guidelines prohibit.")
    text = "Each one is marked below with the clause it conflicts with and a compliant alternative."
    if any(f["serious"] for f in findings):
        text += " Some are claims the guidelines treat as serious violations."
    return text


def run_check(extraction: Extraction, *, publisher: str = "rmp", channel: str = "social", ai_used: str = "no",
              settings: Settings | None = None, client=None, filename: str | None = None) -> dict:
    settings = settings or Settings.from_env()
    publisher = publisher if publisher in PUBLISHERS else "rmp"
    channel = channel if channel in CHANNELS else "other"
    ai_used = ai_used if ai_used in AI_USED else "no"
    started = time.monotonic()
    text = extraction.text

    def nothing_to_check(extra: str = "") -> NothingToCheck:
        detail = " ".join(extraction.warnings + ([extra] if extra else []))
        return NothingToCheck(f"No text could be found in this content, so there is nothing to check. {detail}".strip())

    # Without text, only the AI can review (it looks at the images); never report "compliant" for unread content.
    if not text.strip() and not (extraction.images and settings.ai_enabled):
        raise nothing_to_check()

    items = _rule_items(text)
    disc = disclosures.check(text, publisher, channel)

    analysis: dict = {"mode": "rules", "model": None, "ai_error": None, "usage": None}
    ai_data = None
    if settings.ai_enabled:
        context = {
            "publisher": publisher, "channel": channel, "ai_used": ai_used, "kind": extraction.kind,
            "disclosure_required": disc.required, "disclosure_hint": disc.as_hint(),
        }
        preflags = [{"id": i["id"], "quote": i["quote"], "title": i["title"], "clauses": i["clauses"]}
                    for i in items]
        try:
            review = ai_review.review(text, images=extraction.images, context=context, preflags=preflags,
                                      settings=settings, client=client)
            ai_data = review.data
            analysis.update(mode="rules+ai", model=review.model, usage=review.usage,
                            ai_seconds=review.seconds, est_cost_usd=review.cost_usd)
        except ai_review.AIReviewError as exc:
            log.warning("AI review failed: %s", exc)
            analysis["ai_error"] = str(exc)

    if not text.strip() and ai_data is None:
        raise nothing_to_check(analysis["ai_error"] or "")

    dismissed: list[dict] = []
    if ai_data:
        findings, dismissed = _merge_ai(text, items, ai_data)
        missing = []
        for item in ai_data.get("missing_disclosures", []):
            kind = item.get("item", "other")
            if kind in {"doctor_name", "qualifications", "registration_number"} and not disc.required:
                continue
            if kind == "ai_source_label" and ai_used == "no":
                continue
            label = disclosures.ITEM_LABELS.get(kind, disclosures.ITEM_LABELS["other"])
            clause = "7.2" if kind == "ai_source_label" else "3.2 Expl. III/IV"
            if all(m["item"] != kind for m in missing) or kind == "other":
                missing.append({"item": kind, "label": label, "detail": item.get("detail", ""), "clause": clause})
    else:
        findings = _group_by_rule(items)
        missing = disc.missing(ai_used)

    findings = _order_and_number(findings + _context_findings(ai_used, channel))
    counts = _phrase_counts(findings)
    counts["disclosures"] = len(missing)
    verdict = _verdict(findings, missing)

    rewrite = (ai_data or {}).get("compliant_rewrite") or None
    if rewrite and squash(rewrite) == squash(text):
        rewrite = None
    serious = any(f["serious"] for f in findings)
    penalty = None
    if verdict != "compliant":
        penalty = {
            "serious": serious,
            "ladder": PENALTY_LADDER,
            "note": ("For hospitals/clinics, action is taken under the applicable Clinical Establishments Act; "
                     "doctors who approved the content can also be examined by the SMC/NMC (clause 11.5)."
                     if publisher in {"hospital", "agency"} else
                     "Action is taken by the State Medical Council after a show-cause notice (clause 10.2)."),
        }

    return {
        "verdict": verdict,
        "headline": _headline(verdict, counts),
        "summary": _summary(verdict, findings, ai_data),
        "content_summary": (ai_data or {}).get("content_summary"),
        "is_advertisement": (ai_data or {}).get("is_advertisement"),
        "counts": counts,
        "findings": findings,
        "missing_disclosures": missing,
        "dismissed": dismissed,
        "highlights": build_highlights(findings),
        "text": text,
        "compliant_rewrite": rewrite,
        "penalty": penalty,
        "source": {"kind": extraction.kind, "filename": filename, "how": extraction.how, "meta": extraction.meta},
        "analysis": analysis,
        "options": {"publisher": publisher, "channel": channel, "ai_used": ai_used},
        "warnings": extraction.warnings,
        "guideline": rule_engine.guideline_reference(),
        "disclaimer": DISCLAIMER,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
