from __future__ import annotations

import http.client
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from nvidia_nim_proxy.server import API_KEY_MODE_POOL, ProxyConfig, build_server


def test_real_http_pool_cycles_six_keys_and_sanitizes_requests() -> None:
    received: list[tuple[str | None, dict[str, Any]]] = []

    class MockNvidiaHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            content_length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(content_length))
            received.append((self.headers.get("Authorization"), body))
            response_body = json.dumps(
                {
                    "id": "mock-response",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, _format: str, *args: Any) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockNvidiaHandler)
    upstream_thread = Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    keys = tuple(f"nvidia-key-{index}" for index in range(1, 7))
    proxy = build_server(
        "127.0.0.1",
        0,
        ProxyConfig(
            upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
            api_key=None,
            api_key_mode=API_KEY_MODE_POOL,
            local_client_key="local-proxy-secret",
            pool_keys=keys,
            queue_wait_seconds=2,
        ),
    )
    proxy_thread = Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()

    request_body = json.dumps(
        {
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
    )

    try:
        for _ in range(7):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                proxy.server_port,
                timeout=2,
            )
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=request_body,
                headers={
                    "Authorization": "Bearer local-proxy-secret",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            assert response.status == 200
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    assert [authorization for authorization, _ in received] == [
        *(f"Bearer {key}" for key in keys),
        "Bearer nvidia-key-1",
    ]
    assert len(received) == 7
    assert all("extra_body" not in body for _, body in received)
    assert all(body["model"] == "z-ai/glm-5.2" for _, body in received)


@pytest.mark.parametrize("mode", ["env", "client", "pool"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "model",
    [
        "moonshotai/kimi-k3",
        "deepseek-ai/deepseek-v4-flash-0731",
        "z-ai/glm-5.3",
        "z-ai/glm-5.3-flash",
    ],
)
def test_reasoning_profiles_over_http_preserve_tools_and_responses(
    mode: str,
    stream: bool,
    model: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    received: list[tuple[str | None, dict[str, Any]]] = []
    tool_calls = [
        {
            "index": 0,
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
    ]
    message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "private-reasoning-\u00e7",
        "tool_calls": tool_calls,
    }
    if stream:
        events = [
            {
                "id": "mock-response",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "reasoning_content": message["reasoning_content"],
                        },
                    }
                ],
            },
            {
                "id": "mock-response",
                "choices": [
                    {"index": 0, "delta": {"tool_calls": tool_calls}, "finish_reason": "tool_calls"}
                ],
            },
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 10}},
        ]
        response_body = (
            b"".join(
                b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"
                for event in events
            )
            + b"data: [DONE]\n\n"
        )
    else:
        response_body = json.dumps(
            {
                "id": "mock-response",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    class MockNvidiaHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.headers.get("Authorization"), body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            # Deliberately split JSON and UTF-8 across socket writes.
            for offset in range(0, len(response_body), 7):
                self.wfile.write(response_body[offset : offset + 7])
                self.wfile.flush()

        def log_message(self, _format: str, *args: Any) -> None:
            return

    caplog.set_level(logging.DEBUG, logger="zcode-nim-proxy")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockNvidiaHandler)
    proxy = build_server(
        "127.0.0.1",
        0,
        ProxyConfig(
            upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
            api_key="test-upstream-secret" if mode == "env" else None,
            api_key_mode=mode,
            pool_keys=("test-upstream-secret",) if mode == "pool" else (),
            local_client_key="test-local-secret" if mode == "pool" else None,
        ),
    )
    threads = [
        Thread(target=s.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        for s in (upstream, proxy)
    ]
    for thread in threads:
        thread.start()
    messages = [
        {"role": "user", "content": "private-prompt"},
        message,
        {"role": "tool", "tool_call_id": "call-1", "content": "private-tool-result"},
    ]
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 1024,
        "stream": stream,
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        "tool_choice": "auto",
        "stream_options": {"include_usage": True},
        "extra_body": {"unknown": "private-extension"},
    }
    connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=3)
    try:
        token = "test-upstream-secret" if mode == "client" else "test-local-secret"
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(body),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == response_body
    finally:
        connection.close()
        for server in (proxy, upstream):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)

    authorization, forwarded = received[0]
    assert authorization == "Bearer test-upstream-secret"
    assert forwarded["messages"] == messages
    assert forwarded["tools"] == body["tools"]
    assert forwarded["stream"] == stream
    assert "extra_body" not in forwarded
    if model == "moonshotai/kimi-k3":
        assert forwarded["reasoning_effort"] == "max"
    elif model.startswith("deepseek-ai/"):
        assert forwarded["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "max"}
        assert "reasoning_effort" not in forwarded
    else:
        assert "reasoning_effort" not in forwarded
        assert "chat_template_kwargs" not in forwarded
    assert "NVIDIA NIM reasoning policy:" in caplog.text
    for secret in (
        "test-upstream-secret",
        "test-local-secret",
        "private-prompt",
        "private-reasoning",
        "private-tool-result",
        "private-extension",
    ):
        assert secret not in caplog.text
