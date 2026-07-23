from __future__ import annotations

import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

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
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": False}
            },
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
