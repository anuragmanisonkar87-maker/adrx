"""A fake Anthropic API for tests: the real SDK talks to an in-memory HTTP transport."""

import json

import httpx2


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message_stream(model: str, text: str, *, thinking: str | None = "Checking each claim against the clauses.",
                   stop_reason: str = "end_turn", usage: dict | None = None) -> httpx2.Response:
    """A streamed Messages API response: optional thinking block, then the JSON text in two chunks."""
    usage = usage or {"input_tokens": 1200, "output_tokens": 800, "cache_read_input_tokens": 5400,
                      "cache_creation_input_tokens": 0}
    events: list[dict] = [{
        "type": "message_start",
        "message": {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {**usage, "output_tokens": 1}},
    }]
    index = 0
    if thinking:
        events += [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": thinking}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
            {"type": "content_block_stop", "index": 0},
        ]
        index = 1
    half = len(text) // 2
    events += [
        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text[:half]}},
        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text[half:]}},
        {"type": "content_block_stop", "index": index},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": usage["output_tokens"]}},
        {"type": "message_stop"},
    ]
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(events))


def error(status: int, err_type: str, message: str) -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": err_type, "message": message}})


def model_info(model: str, name: str) -> httpx2.Response:
    return httpx2.Response(200, json={"type": "model", "id": model, "display_name": name,
                                      "created_at": "2026-08-01T00:00:00Z"})


def client_kwargs(handler) -> dict:
    """kwargs for app.engine.ai_review.make_client / anthropic.Anthropic that route to ``handler``."""
    return {"base_url": "http://anthropic.test", "http_client": httpx2.Client(transport=httpx2.MockTransport(handler))}
