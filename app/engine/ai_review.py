"""AI review with Claude: confirms rule flags, finds subtler conflicts and drafts a compliant rewrite."""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.config import Settings

log = logging.getLogger(__name__)

GUIDELINES_FILE = Path(__file__).resolve().parent.parent / "knowledge" / "nmc_guidelines_2026.md"

# USD per million tokens: input, output, cache read, 5-minute cache write (Anthropic's published prices).
# Only used to log an approximate cost per check; update if prices change.
PRICES_PER_MTOK = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}

_FINDING = {
    "type": "object",
    "properties": {
        "quote": {"type": "string"},
        "location": {"type": "string", "enum": ["text", "visual"]},
        "title": {"type": "string"},
        "clause": {"type": "string"},
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "why": {"type": "string"},
        "fix": {"type": "string"},
    },
    "required": ["quote", "location", "title", "clause", "severity", "why", "fix"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "content_summary": {"type": "string"},
        "is_advertisement": {"type": "boolean"},
        "verdict": {"type": "string", "enum": ["compliant", "needs_changes", "non_compliant"]},
        "verdict_reason": {"type": "string"},
        "findings": {"type": "array", "items": _FINDING},
        "preflag_reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "flag_id": {"type": "string"},
                    "valid": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["flag_id", "valid", "note"],
                "additionalProperties": False,
            },
        },
        "missing_disclosures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "enum": ["doctor_name", "qualifications", "registration_number", "ai_source_label", "other"],
                    },
                    "detail": {"type": "string"},
                },
                "required": ["item", "detail"],
                "additionalProperties": False,
            },
        },
        "compliant_rewrite": {"type": "string"},
    },
    "required": [
        "content_summary",
        "is_advertisement",
        "verdict",
        "verdict_reason",
        "findings",
        "preflag_reviews",
        "missing_disclosures",
        "compliant_rewrite",
    ],
    "additionalProperties": False,
}

PUBLISHER_LABELS = {
    "rmp": "a Registered Medical Practitioner (doctor)",
    "hospital": "a hospital / clinic / medical institution",
    "agency": "a marketing agency or third party on behalf of a doctor or hospital",
    "other": "someone else (not a doctor or hospital)",
}
CHANNEL_LABELS = {
    "social": "social media (electronic media)",
    "youtube": "YouTube / video platform (electronic media)",
    "website": "website or online listing (electronic media)",
    "whatsapp": "WhatsApp / messaging (electronic media)",
    "tv_radio": "TV or radio (electronic media)",
    "print": "newspaper, magazine, pamphlet or other print",
    "hoarding": "hoarding / billboard in a public space (clause 5.2 applies)",
    "other": "not specified",
}
AI_USED_LABELS = {"yes": "yes", "no": "no", "unsure": "not sure"}
SOURCE_NOTES = {
    "text": "pasted or uploaded text",
    "pdf": "a PDF (text layer, scanned pages read by OCR)",
    "image": "an image; the text below was read by OCR and may contain OCR errors",
    "docx": "a Word document",
    "audio": "an audio recording; the text below is an automatic speech transcript and may contain errors",
    "video": "a video; the text below is an automatic speech transcript plus on-screen text read by OCR, "
    "both may contain recognition errors",
}


class AIReviewError(RuntimeError):
    pass


@dataclass
class AIReview:
    data: dict
    model: str
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0
    cost_usd: float | None = None


# ---------------------------------------------------------------- prompts


@lru_cache(maxsize=1)
def system_prompt() -> str:
    guidelines = GUIDELINES_FILE.read_text(encoding="utf-8")
    return f"""You are an advertising-compliance reviewer for doctors (Registered Medical Practitioners, \
RMPs) and hospitals/clinics in India. You check content against the National Medical Commission (NMC) \
"Guidelines on Ethical Advertising and Public Communication" (Public Notice R-13014/01/2024-Ethics, \
06-10-2026), reproduced below. Base every finding on these guidelines and cite their clause numbers.

<guidelines>
{guidelines}
</guidelines>

How to review:
1. Decide whether the content is promotional/advertising (clause 3.2). Remember what is permitted: factual,
   verifiable institutional information (8.3(i), 9.1), formal announcements (8.2(ii), 9.3), public-health
   education that does not promote a practice (3.2 Expl. I, 5.1, 8.2(iii)), factual fee disclosure, and
   Government programme/scheme campaigns (exempt, 7.3 Explanation). Do not flag permitted factual content.
2. Find every word or phrase that conflicts with the guidelines. Copy each quote EXACTLY from the content
   (same script, spelling, capitalisation and punctuation) and keep it short - just the offending phrase -
   so it can be highlighted. One finding per distinct problem.
3. For each finding give: a short title; the most specific clause (e.g. "8.1(iii)", "8.1(ix)",
   "3.2 Expl. V", "6.4", "7.2"); "why" in one or two plain-English sentences a busy doctor understands;
   and "fix" with concrete compliant wording to use instead.
4. Severity: high = clearly prohibited (guarantees or cure claims, superiority claims, success rates or
   patient counts, testimonials/reviews/ratings, before-after, discounts/offers/freebies, product
   endorsement, commissions, fake engagement, celebrity endorsement); medium = likely problematic or
   needs verification (solicitation calls-to-action, fear-based messaging, awards without verifiable
   details, self-promotional praise, possible patient identification); low = advisory.
5. Review every automatic pre-flag: valid=true only if the phrase really conflicts in this context
   (false positives include "best wishes", "toll-free", "No. 1" in an address, negated or educational uses).
6. Missing disclosures: when clause 3.2 Expl. III/IV applies (electronic media post by or for an RMP or
   hospital), list missing items: doctor's name, qualifications, registration status/SMC-NMR number. When
   AI was used, list a missing "AI-generated" source mark (7.2). Information visible in attached images
   counts as present.
7. If images or video frames are attached, also check what they show: before/after photos, identifiable
   patients (faces, names), a doctor's photo used for self-promotion (9.2), celebrities, star ratings or
   testimonials shown visually. For purely visual findings use location "visual" and an empty quote.
8. compliant_rewrite: rewrite the whole content so it complies, in the same language and script mix as
   the original, keeping the factual information (services, timings, address, contact, fees stated
   plainly). Use placeholders such as [Doctor's name], [Qualification], [SMC/NMR Reg. No.] for missing
   disclosures. If the content already complies, return it unchanged. If it is not medical
   communication at all, return an empty string.
Be precise and fair. Do not invent text that is not in the content. Write all explanations in simple
English."""


def _user_prompt(text: str, context: dict, preflags: list[dict], n_images: int) -> str:
    flags = "\n".join(
        f'{f["id"]} | "{f["quote"]}" | {f["title"]} | {", ".join(f["clauses"])}' for f in preflags
    ) or "(none)"
    image_note = f" {n_images} image(s)/frame(s) from it are attached." if n_images else ""
    return f"""Check this content against the NMC guidelines.

<context>
Published by: {PUBLISHER_LABELS.get(context["publisher"], context["publisher"])}
Where it will appear: {CHANNEL_LABELS.get(context["channel"], context["channel"])}
AI used to create it: {AI_USED_LABELS.get(context["ai_used"], context["ai_used"])}
Source: {SOURCE_NOTES.get(context["kind"], context["kind"])}.{image_note}
Clause 3.2 Expl. III/IV disclosure applies: {"yes" if context["disclosure_required"] else "no"}
Automatic disclosure scan: {context["disclosure_hint"]}
</context>

<automatic_preflags>
{flags}
</automatic_preflags>

<content>
{text if text.strip() else "(no text could be extracted - review the attached images)"}
</content>"""


# ---------------------------------------------------------------- client, errors, cost


def make_client(settings: Settings, timeout: float | None = None, max_retries: int = 2, **client_kwargs):
    """Anthropic client. Sends the workspace header when ANTHROPIC_WORKSPACE_ID is set
    (needed only for API keys that are not scoped to a workspace). Extra kwargs go to the SDK client."""
    import anthropic

    headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    client_kwargs.setdefault("base_url", settings.anthropic_base_url)
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        timeout=timeout or settings.ai_timeout_s,
        max_retries=max_retries,
        default_headers=headers,
        **client_kwargs,
    )


def _error_message(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = (body.get("error") or {}).get("message")
        if message:
            return str(message)
    return str(getattr(exc, "message", "") or exc)


def friendly_error(exc: Exception, model: str) -> str:
    """Turn an Anthropic SDK error into a message that says what to fix."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return str(exc)
    detail = _error_message(exc)
    low = detail.lower()
    busy = tuple(
        getattr(anthropic, name)
        for name in ("InternalServerError", "OverloadedError", "ServiceUnavailableError")
        if hasattr(anthropic, name)
    )
    if isinstance(exc, anthropic.AuthenticationError):
        return ("The Anthropic API key was rejected (wrong, revoked or expired). "
                "Check ANTHROPIC_API_KEY in your .env file.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"This API key is not allowed to use {model}. Check the key's workspace and permissions."
    if isinstance(exc, anthropic.NotFoundError):
        if "workspace" in low:
            return "The workspace in ANTHROPIC_WORKSPACE_ID was not found or this key cannot use it."
        return f"Model '{model}' is not available to this API key. Set AI_REVIEW_MODEL to a model your account can use."
    if isinstance(exc, anthropic.RateLimitError):
        return "AI review rate limit reached; please retry in a minute."
    if isinstance(exc, anthropic.BadRequestError):
        if "credit balance" in low:
            return "Your Anthropic account is out of credits. Add credits in the Claude Console under Billing."
        if "not scoped to a workspace" in low or "anthropic-workspace-id" in low:
            return ("This API key is not tied to a workspace. Create a key inside a workspace, or set "
                    "ANTHROPIC_WORKSPACE_ID (Claude Console > Settings > Workspaces > ID column).")
        return f"The AI review request was rejected: {detail[:200]}"
    if busy and isinstance(exc, busy):
        return "Claude is busy right now; please retry in a minute."
    if isinstance(exc, anthropic.APITimeoutError):
        return "The AI review timed out."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check the server's internet connection."
    if isinstance(exc, anthropic.APIStatusError):
        return f"AI review failed (HTTP {exc.status_code}): {detail[:200]}"
    return f"AI review failed: {detail[:200]}"


def estimate_cost_usd(model: str, usage: dict) -> float | None:
    price = PRICES_PER_MTOK.get(model) or next(
        (p for name, p in PRICES_PER_MTOK.items() if model.startswith(name)), None
    )
    if not price or not usage:
        return None
    inp, out, cache_read, cache_write = price
    total = (
        (usage.get("input_tokens") or 0) * inp
        + (usage.get("output_tokens") or 0) * out
        + (usage.get("cache_read_input_tokens") or 0) * cache_read
        + (usage.get("cache_creation_input_tokens") or 0) * cache_write
    ) / 1_000_000
    return round(total, 4)


def verify_access(settings: Settings, client=None) -> dict:
    """Free check that the key works and the configured model is available (no tokens are used)."""
    if not settings.ai_enabled:
        reason = "AI_REVIEW is off" if settings.anthropic_api_key else "No ANTHROPIC_API_KEY set"
        return {"status": "off", "message": f"{reason}: quick check (phrase rules) only."}
    try:
        client = client or make_client(settings, timeout=20, max_retries=1)
        info = client.models.retrieve(settings.claude_model)
        name = getattr(info, "display_name", None) or getattr(info, "id", settings.claude_model)
        return {"status": "ok", "model": getattr(info, "id", settings.claude_model), "model_name": name,
                "message": f"AI review is ready ({name})."}
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return {"status": "error", "message": friendly_error(exc, settings.claude_model)}


# ---------------------------------------------------------------- review


def review(text: str, *, images: list[bytes], context: dict, preflags: list[dict], settings: Settings,
           client=None) -> AIReview:
    """Run the Claude review (streamed, so long reviews never hit HTTP timeouts).
    ``client`` can be injected for tests."""
    client = client or make_client(settings)
    images = images[: settings.ai_max_images]
    content: list[dict] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(img).decode("ascii")}}
        for img in images
    ]
    content.append({"type": "text", "text": _user_prompt(text, context, preflags, len(images))})

    output_config: dict = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    if settings.claude_effort:
        output_config["effort"] = settings.claude_effort
    request = {
        "model": settings.claude_model,
        "max_tokens": settings.ai_max_tokens,
        # the guidelines are identical on every call, so cache them
        "system": [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "output_config": output_config,
    }
    started = time.monotonic()
    response = _call(client, request)
    seconds = time.monotonic() - started

    if response.stop_reason == "refusal":
        raise AIReviewError("The AI reviewer declined to review this content.")
    if response.stop_reason == "max_tokens":
        raise AIReviewError("The AI review was cut off. Try shorter content or raise AI_MAX_TOKENS.")
    raw = next((block.text for block in response.content if getattr(block, "type", "") == "text"), None)
    if not raw:
        raise AIReviewError("The AI reviewer returned no result.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIReviewError("The AI reviewer returned an unreadable result.") from exc

    raw_usage = getattr(response, "usage", None)
    usage = {
        key: getattr(raw_usage, key, None)
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    }
    model = getattr(response, "model", None) or settings.claude_model
    cost = estimate_cost_usd(model, usage)
    log.info("AI review: model=%s %.1fs usage=%s est_cost_usd=%s", model, seconds, usage, cost)
    return AIReview(data=data, model=model, usage=usage, seconds=round(seconds, 1), cost_usd=cost)


def _stream(client, request: dict):
    with client.messages.stream(**request) as stream:
        return stream.get_final_message()


def _call(client, request: dict):
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        anthropic = None
    try:
        return _stream(client, request)
    except Exception as exc:
        is_bad_request = anthropic is not None and isinstance(exc, anthropic.BadRequestError)
        if is_bad_request and "effort" in request["output_config"] and "effort" in _error_message(exc).lower():
            # Some models do not accept an effort level; retry once with the model default.
            retry = dict(request, output_config={"format": request["output_config"]["format"]})
            try:
                return _stream(client, retry)
            except Exception as exc2:  # noqa: BLE001
                raise AIReviewError(friendly_error(exc2, request["model"])) from exc2
        raise AIReviewError(friendly_error(exc, request["model"])) from exc
