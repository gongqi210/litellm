from __future__ import annotations

import json

from aimanager.scripts.mock_ycapi import build_mock_response


def test_mock_ycapi_returns_openai_compatible_model_list() -> None:
    status, headers, body = build_mock_response("/v1/models", {})

    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert payload["object"] == "list"
    assert [model["id"] for model in payload["data"]] == [
        "gemini-2.5-flash",
        "deepseek-chat",
        "ycapi-image-1",
        "ycapi-video-1",
    ]
    assert all(model["object"] == "model" for model in payload["data"])


def test_mock_ycapi_returns_video_job_with_usage_duration() -> None:
    status, headers, body = build_mock_response(
        "/v1/videos", {"model": "ycapi-video-1", "prompt": "a calm ocean", "seconds": "5"}
    )

    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert payload["object"] == "video"
    assert payload["model"] == "ycapi-video-1"
    assert payload["usage"]["duration_seconds"] == 5

    poll_status, _headers, poll_body = build_mock_response("/v1/videos/video-mock-1", {})
    poll = json.loads(poll_body.decode("utf-8"))
    assert poll_status == 200
    assert poll["id"] == "video-mock-1"
    assert poll["status"] == "completed"


def test_mock_ycapi_returns_openai_compatible_chat_completion() -> None:
    status, headers, body = build_mock_response(
        "/v1/chat/completions",
        {
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "gemini-2.5-flash"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert payload["usage"]["prompt_tokens"] > 0
    assert payload["usage"]["completion_tokens"] > 0
    assert payload["usage"]["total_tokens"] == (
        payload["usage"]["prompt_tokens"] + payload["usage"]["completion_tokens"]
    )


def test_mock_ycapi_streams_usage_when_include_usage_is_true() -> None:
    status, headers, body = build_mock_response(
        "/v1/chat/completions",
        {
            "model": "gemini-2.5-flash",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    events = _decode_sse_events(body)
    usage_events = [event for event in events if isinstance(event, dict) and "usage" in event]
    assert status == 200
    assert headers["content-type"] == "text/event-stream"
    assert events[-1] == "[DONE]"
    assert usage_events
    assert usage_events[-1]["usage"]["prompt_tokens"] > 0
    assert usage_events[-1]["usage"]["total_tokens"] == (
        usage_events[-1]["usage"]["prompt_tokens"] + usage_events[-1]["usage"]["completion_tokens"]
    )


def test_mock_ycapi_stream_omits_usage_without_include_usage() -> None:
    status, headers, body = build_mock_response(
        "/v1/chat/completions",
        {
            "model": "gemini-2.5-flash",
            "stream": True,
            "stream_options": {"include_usage": False},
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    events = _decode_sse_events(body)
    assert status == 200
    assert headers["content-type"] == "text/event-stream"
    assert events[-1] == "[DONE]"
    assert not [event for event in events if isinstance(event, dict) and "usage" in event]


def test_mock_ycapi_returns_openai_compatible_image_generation() -> None:
    status, headers, body = build_mock_response(
        "/v1/images/generations",
        {
            "model": "ycapi-image-1",
            "prompt": "runtime smoke",
            "n": 2,
        },
    )

    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert payload["created"] > 0
    assert len(payload["data"]) == 2
    assert payload["data"][0]["url"].startswith("https://example.invalid/aimanager-smoke/")


def test_mock_ycapi_returns_b64_json_when_requested_for_image_generation() -> None:
    status, _headers, body = build_mock_response(
        "/v1/images/generations",
        {
            "model": "ycapi-image-1",
            "prompt": "runtime smoke",
            "response_format": "b64_json",
        },
    )

    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert payload["data"][0]["b64_json"]
    assert "url" not in payload["data"][0]


def test_mock_ycapi_returns_openai_error_for_unknown_path() -> None:
    status, _headers, body = build_mock_response("/v1/embeddings", {"model": "text"})

    payload = json.loads(body.decode("utf-8"))
    assert status == 404
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["code"] == "ycapi_mock_route_not_found"


def _decode_sse_events(body: bytes) -> list[dict[str, object] | str]:
    events: list[dict[str, object] | str] = []
    for line in body.decode("utf-8").splitlines():
        if not line.startswith("data: "):
            continue
        data = line.removeprefix("data: ")
        events.append(data if data == "[DONE]" else json.loads(data))
    return events
