import json

import pytest

from tests import fake_anthropic as fake

from app.config import Settings
from app.engine.ai_review import make_client, verify_access
from app.engine.checker import build_highlights, locate, run_check
from app.extract import Extraction, from_text

AD = (
    "Dr. Rajesh Mehta - Mumbai's No.1 Hair Transplant Surgeon!\n"
    "100% guaranteed results with our painless FUE technique. 10,000+ happy patients.\n"
    "DIWALI OFFER: Flat 40% OFF + FREE consultation. Limited slots - book your appointment now!\n"
    "With best wishes, Team Mehta Clinic"
)


def settings(**overrides) -> Settings:
    base = Settings.from_env()
    return Settings(**{**base.__dict__, **overrides})


def check(text, **kw):
    s = kw.pop("settings", None) or settings()
    return run_check(from_text(text, s), settings=s, **kw)


def test_rules_only_verdict_and_grouping():
    result = check(AD)
    assert result["verdict"] == "non_compliant"
    assert result["analysis"]["mode"] == "rules"
    offers = [f for f in result["findings"] if f["rule_id"] == "inducement_offer"]
    assert len(offers) == 1, "one card per rule"
    assert {q["text"] for q in offers[0]["quotes"]} >= {"DIWALI OFFER", "Flat 40% OFF", "FREE consultation"}
    assert result["penalty"]["serious"] is True
    assert {m["item"] for m in result["missing_disclosures"]} == {"qualifications", "registration_number"}
    # every quote points at the right text
    for f in result["findings"]:
        for q in f["quotes"]:
            for s, e in q["spans"]:
                assert result["text"][s:e] == q["text"]


def test_compliant_notice():
    notice = ("Dr. Anita Shah, MBBS, MD (General Medicine), Reg. No. MMC 2009/04/1234\n"
              "We have moved to 2nd floor, Sai Plaza, SV Road, Andheri West. OPD: Mon-Sat 10 am-1 pm. Fee: ₹600.")
    result = check(notice)
    assert result["verdict"] == "compliant"
    assert result["findings"] == [] and result["missing_disclosures"] == []
    assert result["penalty"] is None


def test_needs_changes_when_only_disclosures_missing():
    result = check("Shah Clinic has moved to SV Road, Andheri West. OPD Mon-Sat 10 am-1 pm.", publisher="hospital")
    assert result["verdict"] == "needs_changes"
    assert result["headline"].endswith("to fix before posting")


def test_ai_used_adds_context_and_label_requirement():
    result = check("Dr. Anita Shah, MBBS, Reg. No. MMC 2009/04/1234. Clinic timings 10-1.", ai_used="yes")
    assert any(f["rule_id"] == "ai_generated" for f in result["findings"])
    assert any(m["item"] == "ai_source_label" for m in result["missing_disclosures"])


def test_highlights_do_not_overlap():
    result = check(AD)
    hl = result["highlights"]
    assert hl, "expected highlights"
    for a, b in zip(hl, hl[1:]):
        assert a["end"] <= b["start"]
    ids = {f["id"] for f in result["findings"]}
    assert all(set(h["finding_ids"]) <= ids for h in hl)


def test_build_highlights_merges_overlaps():
    findings = [
        {"id": "F1", "severity": "high", "spans": [[0, 10]]},
        {"id": "F2", "severity": "medium", "spans": [[5, 15]]},
    ]
    assert build_highlights(findings) == [
        {"start": 0, "end": 5, "severity": "high", "finding_ids": ["F1"]},
        {"start": 5, "end": 10, "severity": "high", "finding_ids": ["F1", "F2"]},
        {"start": 10, "end": 15, "severity": "medium", "finding_ids": ["F2"]},
    ]


def test_locate_is_whitespace_and_case_tolerant():
    text = "Flat 40%\nOFF on all\nprocedures"
    assert locate(text, "flat 40% off") == [[0, 12]]
    assert locate(text, '"Flat 40% OFF"') == [[0, 12]]
    assert locate(text, "not there") == []


def test_nothing_to_check():
    from app.engine.checker import NothingToCheck

    with pytest.raises(NothingToCheck):
        run_check(Extraction(kind="image", text=""), settings=settings())


# ---------- AI review through the real Anthropic SDK with a mocked HTTP layer ----------

FAKE_REVIEW = {
    "content_summary": "Social media ad for a hair transplant clinic.",
    "is_advertisement": True,
    "verdict": "non_compliant",
    "verdict_reason": "The ad uses guarantees, superiority claims, discounts and patient counts.",
    "findings": [
        {"quote": "Mumbai's No.1 Hair Transplant Surgeon", "location": "text", "title": "Superiority claim",
         "clause": "8.1(ix)", "severity": "high", "why": "No.1 is an unverifiable superiority claim.",
         "fix": "Hair transplant surgeon, Andheri West"},
        {"quote": "100% guaranteed results", "location": "text", "title": "Guaranteed outcome",
         "clause": "8.1(iii)", "severity": "high", "why": "Guarantees of results are prohibited.",
         "fix": "Results vary; consult the doctor."},
        {"quote": "DIWALI OFFER: Flat 40% OFF + FREE consultation", "location": "text", "title": "Festive discount",
         "clause": "8.1(x)", "severity": "high", "why": "Discounts and free consultations are inducements.",
         "fix": "Consultation fee: Rs 800"},
        {"quote": "", "location": "visual", "title": "Doctor's photo used for promotion", "clause": "9.2",
         "severity": "medium", "why": "The poster uses the doctor's photograph for self-promotion.",
         "fix": "Remove the photograph."},
    ],
    "preflag_reviews": [],  # filled in per test from the request
    "missing_disclosures": [
        {"item": "qualifications", "detail": "Add qualifications."},
        {"item": "registration_number", "detail": "Add SMC/NMR registration number."},
    ],
    "compliant_rewrite": "Dr. Rajesh Mehta, [Qualification], [SMC/NMR Reg. No.]\nHair transplant (FUE) services.",
}


class Recorder:
    """Plays the Anthropic API: records each request and streams back a review."""

    def __init__(self, review_for):
        self.review_for = review_for
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        return fake.message_stream(body["model"], json.dumps(self.review_for(body)))


def fake_client(handler, s=None):
    return make_client(s or settings(anthropic_api_key="sk-ant-test"), max_retries=0, **fake.client_kwargs(handler))


def review_marking(invalid_phrase):
    def review_for(body):
        prompt = body["messages"][0]["content"][-1]["text"]
        flags = prompt.split("<automatic_preflags>")[1].split("</automatic_preflags>")[0].strip().splitlines()
        reviews = []
        for line in flags:
            flag_id, quote = line.split(" | ")[0], line.split(" | ")[1].strip('"')
            valid = quote.lower() != invalid_phrase
            reviews.append({"flag_id": flag_id, "valid": valid, "note": "" if valid else "Used as a greeting."})
        return dict(FAKE_REVIEW, preflag_reviews=reviews)
    return review_for


def test_ai_review_request_and_merge():
    recorder = Recorder(review_marking(invalid_phrase="best"))
    s = settings(anthropic_api_key="sk-ant-test", claude_model="claude-sonnet-5-5", claude_effort="medium")
    result = run_check(from_text(AD, s), settings=s, client=fake_client(recorder, s), publisher="rmp",
                       channel="social")

    # request shape
    req = recorder.requests[0]
    assert req["url"].endswith("/v1/messages")
    body = req["body"]
    assert body["stream"] is True
    assert body["model"] == "claude-sonnet-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium"
    assert "temperature" not in body
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<guidelines>" in body["system"][0]["text"]
    assert "discounts, limited-period offers" in body["system"][0]["text"]
    assert body["messages"][0]["content"][-1]["type"] == "text"
    assert "<automatic_preflags>" in body["messages"][0]["content"][-1]["text"]
    assert "anthropic-workspace-id" not in req["headers"]

    # merge
    analysis = result["analysis"]
    assert analysis["mode"] == "rules+ai"
    assert analysis["usage"]["cache_read_input_tokens"] == 5400
    assert analysis["est_cost_usd"] == pytest.approx((1200 * 2 + 800 * 10 + 5400 * 0.10) / 1e6, abs=1e-4)
    assert result["verdict"] == "non_compliant"
    assert result["summary"] == FAKE_REVIEW["verdict_reason"]
    titles = [f["title"] for f in result["findings"]]
    assert "Festive discount" in titles and "Doctor's photo used for promotion" in titles
    discount = next(f for f in result["findings"] if f["title"] == "Festive discount")
    assert discount["source"] == "rules+ai" and discount["serious"] is True
    assert discount["spans"] and result["text"][discount["spans"][0][0]:discount["spans"][0][1]].startswith("DIWALI")
    visual = next(f for f in result["findings"] if f["location"] == "visual")
    assert visual["spans"] == []
    # rule hits the AI did not list individually are still reported (e.g. painless)
    assert any(f["rule_id"] == "painless_miracle" for f in result["findings"])
    assert result["compliant_rewrite"].startswith("Dr. Rajesh Mehta")
    assert {m["item"] for m in result["missing_disclosures"]} == {"qualifications", "registration_number"}


def test_ai_dismissed_flags_are_reported():
    text = "Shop No. 1 is our pharmacy. Best skin care advice: use sunscreen daily."
    recorder = Recorder(review_marking(invalid_phrase="best"))
    s = settings(anthropic_api_key="sk-ant-test")
    result = run_check(from_text(text, s), settings=s, client=fake_client(recorder, s), channel="print")
    assert [d["quote"] for d in result["dismissed"]] == ["Best"]
    assert all(q["text"] != "Best" for f in result["findings"] for q in f.get("quotes", []))


def test_workspace_header_sent_when_configured():
    recorder = Recorder(review_marking(invalid_phrase=""))
    s = settings(anthropic_api_key="sk-ant-test", anthropic_workspace_id="wrkspc_01TEST")
    run_check(from_text(AD, s), settings=s, client=fake_client(recorder, s))
    assert recorder.requests[0]["headers"]["anthropic-workspace-id"] == "wrkspc_01TEST"


@pytest.mark.parametrize(
    "status, err_type, message, expected",
    [
        (401, "authentication_error", "invalid x-api-key", "The Anthropic API key was rejected"),
        (400, "invalid_request_error",
         "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing.",
         "out of credits"),
        (400, "invalid_request_error",
         "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header",
         "ANTHROPIC_WORKSPACE_ID"),
        (404, "not_found_error", "model: claude-sonnet-9", "is not available to this API key"),
        (429, "rate_limit_error", "Number of requests has exceeded your rate limit", "rate limit"),
        (529, "overloaded_error", "Overloaded", "Claude is busy"),
    ],
)
def test_ai_errors_fall_back_to_rules_with_a_clear_message(status, err_type, message, expected):
    s = settings(anthropic_api_key="sk-ant-test")
    client = fake_client(lambda request: fake.error(status, err_type, message), s)
    result = run_check(from_text(AD, s), settings=s, client=client)
    assert result["analysis"]["mode"] == "rules"
    assert expected in result["analysis"]["ai_error"]
    assert result["verdict"] == "non_compliant"


def test_effort_rejected_is_retried_without_effort():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if "effort" in body.get("output_config", {}):
            return fake.error(400, "invalid_request_error", "output_config.effort: not supported for this model")
        review = dict(FAKE_REVIEW, findings=[], preflag_reviews=[], missing_disclosures=[], verdict="compliant",
                      compliant_rewrite="")
        return fake.message_stream(body["model"], json.dumps(review), thinking=None)

    s = settings(anthropic_api_key="sk-ant-test", claude_effort="medium")
    result = run_check(from_text("OPD timings 10 am to 1 pm.", s), settings=s, client=fake_client(handler, s),
                       channel="print")
    assert len(calls) == 2 and "effort" not in calls[1]["output_config"]
    assert result["analysis"]["mode"] == "rules+ai"


def test_cut_off_review_is_reported():
    s = settings(anthropic_api_key="sk-ant-test")
    client = fake_client(lambda r: fake.message_stream(s.claude_model, '{"content_summary": "x', stop_reason="max_tokens"), s)
    result = run_check(from_text(AD, s), settings=s, client=client)
    assert result["analysis"]["mode"] == "rules"
    assert "cut off" in result["analysis"]["ai_error"]


def test_verify_access_ok_and_errors():
    s = settings(anthropic_api_key="sk-ant-test", claude_model="claude-sonnet-5-5")
    seen = []

    def ok(request):
        seen.append(str(request.url))
        return fake.model_info("claude-sonnet-5-5", "Claude Sonnet 5.5")

    status = verify_access(s, client=fake_client(ok, s))
    assert status == {"status": "ok", "model": "claude-sonnet-5-5", "model_name": "Claude Sonnet 5.5",
                      "message": "AI review is ready (Claude Sonnet 5.5)."}
    assert seen[0].endswith("/v1/models/claude-sonnet-5-5")

    bad = verify_access(s, client=fake_client(lambda r: fake.error(401, "authentication_error", "invalid"), s))
    assert bad["status"] == "error" and "rejected" in bad["message"]
    assert verify_access(settings())["status"] == "off"
