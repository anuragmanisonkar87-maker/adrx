"""Check that the AI review works with your real Anthropic API key.

    python -m app.ai_check                       # key + model check (free), then one real check on a sample ad
    python -m app.ai_check --quick               # key + model check only (free)
    python -m app.ai_check --file samples/poster_en.png   # real check on any file (tests images/OCR too)

Exit code 0 = AI review works, 1 = something to fix (the message says what).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

SAMPLE_AD = """Dr. Rajesh Mehta - Mumbai's No.1 Hair Transplant Surgeon!
100% guaranteed results with our painless FUE technique. 10,000+ happy patients.
DIWALI OFFER: Flat 40% OFF + FREE consultation. Limited slots - book your appointment now!
With best wishes, Team Mehta Clinic"""

SEVERITY = {"high": "prohibited", "medium": "fix", "low": "advisory"}
SOURCE = {"rules": "phrase rule", "ai": "AI", "rules+ai": "rule + AI"}


def _say(tag: str, text: str) -> None:
    print(f"   [{tag:<4}] {text}")


def _mask(key: str) -> str:
    return f"{key[:10]}...{key[-4:]}" if len(key) > 16 else "(too short)"


def main(argv: list[str] | None = None) -> int:
    try:  # Windows consoles: never crash on Hindi text or the rupee sign
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.getLogger("app").setLevel(logging.CRITICAL)  # this command prints its own clear messages
    parser = argparse.ArgumentParser(description="Check the Claude (Anthropic) integration with your real key.")
    parser.add_argument("--quick", action="store_true", help="only verify the key and model (free)")
    parser.add_argument("--file", type=Path, help="run the real check on this file instead of the sample ad")
    args = parser.parse_args(argv)

    from app import extract
    from app.config import Settings
    from app.engine import ai_review
    from app.engine.checker import run_check

    settings = Settings.from_env()
    print("AdRx - AI review check\n")
    print("1) Settings")
    if not settings.anthropic_api_key:
        _say("FAIL", "ANTHROPIC_API_KEY is not set. Put it in the .env file in the project folder "
                     "(copy .env.example to .env), then run this again.")
        return 1
    _say("OK", f"ANTHROPIC_API_KEY found ({_mask(settings.anthropic_api_key)})")
    if not settings.anthropic_api_key.startswith("sk-ant-"):
        _say("WARN", "Claude API keys start with 'sk-ant-'. Check that the whole key was copied.")
    _say("OK", f"Model: {settings.claude_model}, effort: {settings.claude_effort or 'model default'}")
    if settings.anthropic_workspace_id:
        _say("INFO", f"Workspace header: {settings.anthropic_workspace_id}")

    print("\n2) Key and model access (free, uses no tokens)")
    status = ai_review.verify_access(settings)
    if status["status"] != "ok":
        _say("FAIL", status["message"])
        return 1
    _say("OK", status["message"])
    if args.quick:
        print("\nKey works. Run without --quick to try one real review.")
        return 0

    print("\n3) One real review (uses a few US cents of credit)")
    if args.file:
        if not args.file.exists():
            _say("FAIL", f"File not found: {args.file}")
            return 1
        extraction = extract.from_file(args.file, args.file.name, settings)
        _say("INFO", f"Read {args.file.name} as {extraction.kind} ({', '.join(extraction.how) or 'no text'})")
    else:
        extraction = extract.from_text(SAMPLE_AD, settings)
    started = time.monotonic()
    result = run_check(extraction, settings=settings, publisher="rmp", channel="social", ai_used="no")
    analysis = result["analysis"]
    if analysis["mode"] != "rules+ai":
        _say("FAIL", analysis.get("ai_error") or "The AI review did not run.")
        return 1
    _say("OK", f"Claude ({analysis['model']}) reviewed it in {time.monotonic() - started:.1f} s")
    print(f"          Verdict: {result['verdict']} - {result['headline']}")
    print(f"          {result['summary']}")
    for f in result["findings"]:
        where = f'"{f["quote"]}"' if f["quote"] else f"({f['location']})"
        clause = f["clauses"][0] if f["clauses"] else "-"
        print(f"          - [{SEVERITY[f['severity']]}] {where} -> clause {clause} ({SOURCE.get(f['source'], f['source'])})")
    for d in result["dismissed"]:
        print(f"          - cleared by AI: \"{d['quote']}\" ({d['note']})")
    for m in result["missing_disclosures"]:
        print(f"          - missing: {m['label']}")
    if result["compliant_rewrite"]:
        print("          Compliant version (first lines):")
        for line in result["compliant_rewrite"].splitlines()[:4]:
            print(f"            {line}")
    usage = analysis.get("usage") or {}
    cost = analysis.get("est_cost_usd")
    tokens = (f"Tokens: {usage.get('input_tokens') or 0:,} in, {usage.get('cache_read_input_tokens') or 0:,} "
              f"from cache, {usage.get('output_tokens') or 0:,} out")
    print(f"          {tokens}" + (f". Estimated cost: ${cost:.3f}" if cost is not None else ""))
    print("\nAll set. Start the app with:  uvicorn app.main:app --reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
