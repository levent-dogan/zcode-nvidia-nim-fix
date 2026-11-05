"""Real loopback HTTP regression checks; no NVIDIA credentials or API calls."""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from nvidia_nim_proxy.credentials import fingerprint_secret
from nvidia_nim_proxy.server import MAX_JSON_DEPTH, ProxyConfig, build_server


@contextmanager
def local_proxy(
    mode: str, respond: Callable[[BaseHTTPRequestHandler], None] | None = None,
) -> Iterator[tuple[int, ProxyConfig, list[bytes]]]:
    received: list[bytes] = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            if respond:
                respond(self)
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Upstream) as upstream:
        config = ProxyConfig(
            f"http://127.0.0.1:{upstream.server_port}/v1", "test-token",
            api_key_mode="pool" if mode == "pool-loop" else mode,
            pool_retry_loop=mode == "pool-loop", local_client_key="test-token",
            pool_keys=("test-upstream",), queue_wait_seconds=5,
        )
        with build_server("127.0.0.1", 0, config) as proxy:
            servers = (upstream, proxy)
            threads = [Thread(target=s.serve_forever, kwargs={"poll_interval": 0.01}) for s in servers]
            for thread in threads:
                thread.start()
            try:
                yield proxy.server_port, config, received
            finally:
                for server in servers:
                    server.shutdown()
                for thread in threads:
                    thread.join(2)


def send(port: int, body: bytes = b'{"model":"test","messages":[]}') -> http.client.HTTPConnection:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("POST", "/v1/chat/completions", body, {"Authorization": "Bearer test-token"})
    return conn


def eventually(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "condition not met before deadline"
        time.sleep(0.01)


@pytest.mark.parametrize("mode", ["env", "client", "pool", "pool-loop"])
def test_disconnected_queued_request_is_removed_without_upstream_call(mode: str) -> None:
    with local_proxy(mode) as (port, config, received):
        queue = config.key_pool or config.request_scheduler
        lease = (
            config.key_pool.acquire(frozenset()) if config.key_pool else
            config.request_scheduler.acquire(fingerprint_secret("test-token"), "test")
        )
        conn = send(port)
        try:
            eventually(lambda: queue.snapshot().queued == 1)
            conn.close()
            # Must cancel before the 5-second admission deadline or active-lease release.
            eventually(lambda: queue.snapshot().queued == 0)
        finally:
            conn.close()
            lease.release()
        assert received == []
        conn = send(port)
        try:
            response = conn.getresponse()
            assert response.status == 200
            assert response.read() == b"{}"
        finally:
            conn.close()
        eventually(lambda: queue.snapshot().active == 0)
        assert len(received) == 1


@pytest.mark.parametrize("mode", ["env", "client", "pool"])
@pytest.mark.parametrize("malformed_headers", [False, True])
def test_broken_upstream_response_returns_502(mode: str, malformed_headers: bool) -> None:
    def respond(handler: BaseHTTPRequestHandler) -> None:
        if malformed_headers:
            handler.wfile.write(b"not an HTTP response\r\n\r\n")
        else:
            handler.send_response(200)
            handler.send_header("Content-Length", "999")
            handler.end_headers()
            handler.wfile.write(b'{"incomplete":')

    with local_proxy(mode, respond) as (port, config, received):
        conn = send(port)
        try:
            response = conn.getresponse()
            assert response.status == 502
            assert b"upstream_request_failed" in response.read()
        finally:
            conn.close()
        assert len(received) == 1
        eventually(lambda: (config.key_pool or config.request_scheduler).snapshot().active == 0)


@pytest.mark.parametrize("body", [
    b'{"messages":' + b'[' * 2000 + b']' * 2000 + b'}',
    b'{"temperature":NaN}', b'{"top_p":Infinity}', b'{"temperature":-Infinity}',
    b'{"temperature":1e999}',
], ids=["deeply-nested", "nan", "infinity", "negative-infinity", "overflowing-number"])
def test_invalid_json_returns_400_without_upstream_call(body: bytes) -> None:
    with local_proxy("env") as (port, _, received):
        conn = send(port, body)
        try:
            response = conn.getresponse()
            assert response.status == 400
            assert b"invalid JSON request body" in response.read()
        finally:
            conn.close()
        assert not received


@pytest.mark.parametrize("depth, expected", [(MAX_JSON_DEPTH, 200), (MAX_JSON_DEPTH + 1, 400)])
def test_json_nesting_limit_is_enforced_before_sanitizing(depth: int, expected: int) -> None:
    arrays = depth - 1  # The top-level object also counts.
    body = b'{"model":"test","messages":[],"extra_body":' + b'[' * arrays + b'null' + b']' * arrays + b'}'
    with local_proxy("env") as (port, _, received):
        conn = send(port, body)
        try:
            response = conn.getresponse()
            assert response.status == expected
            response.read()
        finally:
            conn.close()
        assert len(received) == (1 if expected == 200 else 0)


@pytest.mark.parametrize("stream", [False, True])
def test_real_tool_arguments_with_markup_are_not_replaced(stream: bool) -> None:
    message = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "test-call", "type": "function", "index": 0,
        "function": {"name": "write_file", "arguments": '{"text":"<tool_call>example</tool_call>"}'},
    }]}
    content = json.dumps({"choices": [{"delta" if stream else "message": message}]}).encode()
    expected = b"data: " + content + b"\n\ndata: [DONE]\n\n" if stream else content

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        handler.send_header("Content-Length", str(len(expected)))
        handler.end_headers()
        handler.wfile.write(expected)

    with local_proxy("pool", respond) as (port, _, _):
        conn = send(port, json.dumps({"model": "test", "messages": [], "stream": stream}).encode())
        try:
            response = conn.getresponse()
            assert response.status == 200
            assert response.read() == expected
        finally:
            conn.close()


@pytest.mark.parametrize("stream", [False, True])
def test_plain_text_tool_markup_still_returns_diagnostic(stream: bool) -> None:
    message = {"role": "assistant", "content": "<tool_call>Read</tool_call>"}
    content = json.dumps({"choices": [{"delta" if stream else "message": message}]}).encode()
    payload = b"data: " + content + b"\n\ndata: [DONE]\n\n" if stream else content

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    with local_proxy("pool", respond) as (port, _, _):
        conn = send(port, json.dumps({"model": "test", "messages": [], "stream": stream}).encode())
        try:
            response = conn.getresponse()
            assert response.status == 200
            assert b"Provider/model compatibility issue" in response.read()
        finally:
            conn.close()
