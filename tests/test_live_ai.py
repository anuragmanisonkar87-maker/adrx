"""Live tests against the real Anthropic API (they cost a few US cents).

Run them with your key in .env:
    RUN_LIVE_AI=1 pytest -m live -v          (Windows PowerShell: $env:RUN_LIVE_AI=1; pytest -m live -v)
"""

import os

import pytest

from app.config import Settings  # importing this loads .env

LIVE_KEY = os.getenv("ANTHROPIC_API_KEY")
LIVE_ENV = Settings.from_env()  # captured before the test fixtures blank the AI settings
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not (os.getenv("RUN_LIVE_AI") == "1" and LIVE_KEY),
                       reason="set RUN_LIVE_AI=1 and ANTHROPIC_API_KEY to run live AI tests"),
]

BAD_AD = """Dr. Rajesh Mehta - Mumbai's No.1 Hair Transplant Surgeon!
100% guaranteed results with our painless FUE technique. 10,000+ happy patients.
DIWALI OFFER: Flat 40% OFF + FREE consultation. Limited slots - book your appointment now!"""

GOOD_NOTICE = """Shah Family Clinic
Dr. Anita Shah, MBBS, MD (General Medicine)
Reg. No. MMC 2009/04/1234, Maharashtra Medical Council
We have moved. From 1 November 2026 the clinic is at 2nd floor, Sai Plaza, SV Road, Andheri West, Mumbai.
OPD timings: Mon-Sat, 10 am-1 pm and 5-8 pm. Consultation fee: Rs 600."""


def live_settings() -> Settings:
    return LIVE_ENV


def test_live_key_and_model():
    from app.engine.ai_review import verify_access

    status = verify_access(live_settings())
    assert status["status"] == "ok", status["message"]


def test_live_review_of_a_non_compliant_ad():
    from app.engine.checker import run_check
    from app.engine.rules import scan
    from app.extract import from_text
    from app.textnorm import normalize

    s = live_settings()
    result = run_check(from_text(BAD_AD, s), settings=s, publisher="rmp", channel="social")
    assert result["analysis"]["mode"] == "rules+ai", result["analysis"]["ai_error"]
    assert result["verdict"] == "non_compliant"
    anchored = [f for f in result["findings"] if f["spans"]]
    assert len(anchored) >= 4, "AI quotes should be found word-for-word in the text"
    assert all(f["clauses"] for f in result["findings"])
    rewrite = result["compliant_rewrite"] or ""
    assert rewrite, "expected a compliant rewrite"
    high_in_rewrite = [h for h in scan(normalize(rewrite)) if h.rule.severity == "high"]
    assert not high_in_rewrite, f"rewrite still has prohibited phrases: {[h.rule.id for h in high_in_rewrite]}"


def test_live_review_of_a_compliant_notice():
    from app.engine.checker import run_check
    from app.extract import from_text

    s = live_settings()
    result = run_check(from_text(GOOD_NOTICE, s), settings=s, publisher="rmp", channel="social")
    assert result["analysis"]["mode"] == "rules+ai", result["analysis"]["ai_error"]
    assert result["verdict"] in {"compliant", "needs_changes"}
    assert not [f for f in result["findings"] if f["severity"] == "high"]


def test_live_review_of_a_poster_image(samples):
    from app import extract
    from app.engine.checker import run_check

    s = live_settings()
    path = samples / "poster_en.png"
    result = run_check(extract.from_file(path, path.name, s), settings=s, publisher="rmp", channel="social")
    assert result["analysis"]["mode"] == "rules+ai", result["analysis"]["ai_error"]
    assert result["verdict"] == "non_compliant"
