from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def build_mock_response(path: str, payload: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
    normalized_path = path.split("?", 1)[0].rstrip("/")
    headers = {"content-type": "application/json"}
    if normalized_path in {"/v1/models", "/models"}:
        body = {
            "object": "list",
            "data": [
                {"id": "gemini-2.5-flash", "object": "model", "owned_by": "ycapi"},
                {"id": "deepseek-chat", "object": "model", "owned_by": "ycapi"},
                {"id": "ycapi-image-1", "object": "model", "owned_by": "ycapi"},
                {"id": "ycapi-video-1", "object": "model", "owned_by": "ycapi"},
            ],
        }
        return 200, headers, _json_bytes(body)

    if normalized_path in {"/v1/chat/completions", "/chat/completions"}:
        model = _string(payload.get("model"), default="gemini-2.5-flash")
        prompt_tokens = _estimate_prompt_tokens(payload.get("messages"))
        completion_tokens = 7
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        if payload.get("stream") is True:
            stream_options = payload.get("stream_options")
            include_usage = isinstance(stream_options, dict) and stream_options.get("include_usage") is True
            headers = {"content-type": "text/event-stream"}
            return 200, headers, _stream_chat_response_bytes(model=model, usage=usage, include_usage=include_usage)
        body = {
            "id": f"chatcmpl-aimanager-mock-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "AiManager ycapi mock response",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
        return 200, headers, _json_bytes(body)

    if normalized_path in {"/v1/images/generations", "/images/generations"}:
        n = _positive_int(payload.get("n"), default=1)
        response_format = _string(payload.get("response_format"), default="url")
        if response_format == "b64_json":
            data = [{"b64_json": "aW1hbmFnZXItbW9jay1pbWFnZQ=="} for _index in range(n)]
        else:
            data = [
                {"url": f"https://example.invalid/aimanager-smoke/{index + 1}.png"}
                for index in range(n)
            ]
        body = {
            "created": int(time.time()),
            "data": data,
        }
        return 200, headers, _json_bytes(body)

    if normalized_path in {"/v1/videos", "/videos"}:
        model = _string(payload.get("model"), default="ycapi-video-1")
        requested_seconds = _string(payload.get("seconds"), default="4")
        duration_seconds = int(requested_seconds) if requested_seconds.isdigit() else 4
        created = int(time.time())
        body = {
            "id": f"video-aimanager-mock-{created}",
            "object": "video",
            "model": model,
            "status": "completed",
            "progress": 100,
            "created_at": created,
            "completed_at": created,
            "seconds": str(duration_seconds),
            "size": _string(payload.get("size"), default="1280x720"),
            "usage": {"duration_seconds": duration_seconds},
        }
        return 200, headers, _json_bytes(body)

    if normalized_path.startswith("/v1/videos/") or normalized_path.startswith("/videos/"):
        video_id = normalized_path.split("/videos/", 1)[1].split("/", 1)[0]
        created = int(time.time())
        body = {
            "id": video_id,
            "object": "video",
            "model": "ycapi-video-1",
            "status": "completed",
            "progress": 100,
            "created_at": created,
            "completed_at": created,
        }
        return 200, headers, _json_bytes(body)

    body = {
        "error": {
            "message": f"ycapi mock route is not implemented: {normalized_path or '/'}",
            "type": "invalid_request_error",
            "param": None,
            "code": "ycapi_mock_route_not_found",
        }
    }
    return 404, headers, _json_bytes(body)


class MockYcapiHandler(BaseHTTPRequestHandler):
    server_version = "AiManagerMockYcapi/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] in {"/health", "/healthz"}:
            self._send_json(200, {"status": "ok"})
            return
        status, headers, body = build_mock_response(self.path, {})
        self._send(status, headers, body)

    def do_POST(self) -> None:  # noqa: N802
        raw_body = self.rfile.read(_content_length(self.headers.get("content-length")))
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                400,
                {
                    "error": {
                        "message": "ycapi mock requires a JSON object request body",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "ycapi_mock_invalid_json",
                    }
                },
            )
            return
        if not isinstance(payload, dict):
            payload = {}
        status, headers, body = build_mock_response(self.path, payload)
        self._send(status, headers, body)

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("ycapi_mock %s %s\n" % (self.command, self.path.split("?", 1)[0]))

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, {"content-type": "application/json"}, _json_bytes(payload))

    def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(*, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), MockYcapiHandler)
    try:
        print(f"ycapi mock listening on http://{host}:{port}", flush=True)
        server.serve_forever()
    finally:
        server.server_close()


def _estimate_prompt_tokens(messages: Any) -> int:
    if not isinstance(messages, list) or not messages:
        return 1
    text_parts: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                text_parts.extend(str(part) for part in content)
    return max(1, len(" ".join(text_parts)) // 4)


def _positive_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return min(value, 10)
    return default


def _string(value: Any, *, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _content_length(value: str | None) -> int:
    try:
        return max(0, int(value or "0"))
    except ValueError:
        return 0


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _stream_chat_response_bytes(*, model: str, usage: dict[str, int], include_usage: bool) -> bytes:
    response_id = f"chatcmpl-aimanager-mock-{int(time.time())}"
    created = int(time.time())
    chunks: list[dict[str, Any] | str] = [
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "AiManager "}, "finish_reason": None}],
        },
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": "ycapi mock response"}, "finish_reason": None}],
        },
    ]
    final_chunk: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    if include_usage:
        final_chunk["usage"] = usage
    chunks.extend([final_chunk, "[DONE]"])
    return b"".join(_sse_data(chunk) for chunk in chunks)


def _sse_data(payload: dict[str, Any] | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return f"data: {data}\n\n".encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an OpenAI-compatible ycapi mock for AiManager smoke tests.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
