"""The pieces that help you switch on AI review: health check, UI status and the ai_check command."""

import json

from fastapi.testclient import TestClient

from app import ai_check, main
from app.engine import ai_review
from tests import fake_anthropic as fake
from tests.test_checker import FAKE_REVIEW, review_marking


def test_health_without_key_reports_off():
    body = TestClient(main.app).get("/api/health?verify_ai=true").json()
    assert body["ai_status"] == "off"


def test_health_reports_working_and_broken_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    main._ai_status_cache.clear()
    monkeypatch.setattr(ai_review, "verify_access", lambda s: {
        "status": "ok", "model": "claude-sonnet-5-5", "model_name": "Claude Sonnet 5.5",
        "message": "AI review is ready (Claude Sonnet 5.5)."})
    client = TestClient(main.app)
    body = client.get("/api/health?verify_ai=true").json()
    assert body["ai_status"] == "ok" and body["ai_model_name"] == "Claude Sonnet 5.5"

    # a different key is checked again (cache is per key) and errors are reported, not raised
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-other")
    monkeypatch.setattr(ai_review, "verify_access", lambda s: {"status": "error", "message": "out of credits"})
    body = client.get("/api/health?verify_ai=true").json()
    assert body["ai_status"] == "error" and body["ai_message"] == "out of credits"
    assert "ai_status" not in client.get("/api/health").json()  # plain health stays free and fast


def test_ai_check_without_key_fails(capsys):
    assert ai_check.main([]) == 1
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out


def test_ai_check_quick(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test-key-123456")
    monkeypatch.setattr(ai_review, "verify_access", lambda s: {"status": "ok", "message": "AI review is ready (X)."})
    assert ai_check.main(["--quick"]) == 0
    out = capsys.readouterr().out
    assert "sk-ant-api...3456" in out and "AI review is ready" in out
    assert "sk-ant-api03-test-key-123456" not in out  # never print the full key


def test_ai_check_full_run_against_fake_api(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test-key-123456")
    review_for = review_marking(invalid_phrase="best")

    def handler(request):
        if request.url.path.startswith("/v1/models/"):
            return fake.model_info("claude-sonnet-5-5", "Claude Sonnet 5.5")
        body = json.loads(request.content)
        return fake.message_stream(body["model"], json.dumps(review_for(body)))

    real_make_client = ai_review.make_client
    monkeypatch.setattr(ai_review, "make_client",
                        lambda s, **kw: real_make_client(s, **{**kw, **fake.client_kwargs(handler)}))
    assert ai_check.main([]) == 0
    out = capsys.readouterr().out
    assert "reviewed it in" in out and "Festive discount" not in out  # prints quotes, not titles
    assert '"DIWALI OFFER: Flat 40% OFF + FREE consultation" -> clause 8.1(x)' in out
    assert "Estimated cost: $" in out
    assert FAKE_REVIEW["compliant_rewrite"].splitlines()[0] in out


def test_ai_check_reports_api_error(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test-key-123456")
    real_make_client = ai_review.make_client
    handler = lambda request: fake.error(400, "invalid_request_error", "Your credit balance is too low.")  # noqa: E731
    monkeypatch.setattr(ai_review, "make_client",
                        lambda s, **kw: real_make_client(s, **{**kw, **fake.client_kwargs(handler)}))
    assert ai_check.main(["--quick"]) == 1
    assert "out of credits" in capsys.readouterr().out
