from __future__ import annotations

import http.client
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from typing import Any

import pytest

from nvidia_nim_proxy.server import (
    NIMProxyHandler,
    ProxyConfig,
    UpstreamAttempt,
    UpstreamRequest,
    build_server,
)

KEYS = tuple(f"test-key-{index}" for index in range(1, 7))
DONE = b"data: [DONE]\n\n"


@contextmanager
def running_pool(
    respond: Callable[[BaseHTTPRequestHandler, int], None],
    **settings: Any,
) -> Iterator[tuple[int, list[str], ProxyConfig]]:
    seen: list[str] = []

    class MockUpstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(self.headers["Authorization"].removeprefix("Bearer "))
            respond(self, len(seen))

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream) as upstream:
        config = ProxyConfig(
            upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
            api_key=None,
            api_key_mode="pool",
            local_client_key="test-local-token",
            **{"pool_retry_loop": True, "pool_keys": KEYS, "max_5xx_failovers": 0, **settings},
        )
        with build_server("127.0.0.1", 0, config) as proxy:
            servers = (upstream, proxy)
            threads = [Thread(target=s.serve_forever, kwargs={"poll_interval": 0.01}) for s in servers]
            for thread in threads:
                thread.start()
            try:
                yield proxy.server_port, seen, config
            finally:
                for server in servers:
                    server.shutdown()
                for thread in threads:
                    thread.join(timeout=2)


def respond_with(
    handler: BaseHTTPRequestHandler, status: int, data: bytes = b"{}", retry_after: str | None = None
) -> None:
    handler.send_response(status)
    if retry_after is not None:
        handler.send_header("Retry-After", retry_after)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def send_request(port: int, *, stream: bool = False) -> http.client.HTTPConnection:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    body = '{"model":"z-ai/glm-5.3","messages":[],"stream":' + ("true}" if stream else "false}")
    conn.request(
        "POST", "/v1/chat/completions", body,
        {"Authorization": "Bearer test-local-token", "Content-Type": "application/json"},
    )
    return conn


def complete_request(port: int, *, stream: bool = False) -> tuple[int, bytes]:
    conn = send_request(port, stream=stream)
    try:
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.mark.parametrize("stream", [False, True])
def test_retry_wraps_to_first_key_and_returns_same_request(
    monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    delays: list[int] = []

    def record_delay(_handler: NIMProxyHandler, seconds: int) -> bool:
        delays.append(seconds)
        return True

    monkeypatch.setattr(NIMProxyHandler, "_wait_for_pool_retry_round", record_delay)
    statuses = (429, 500, 502, 503, 504, 529, 200)
    expected = DONE if stream else b'{"ok":true}'

    def respond(handler: BaseHTTPRequestHandler, number: int) -> None:
        respond_with(handler, statuses[number - 1], expected, retry_after="0")

    with running_pool(respond) as (port, seen, _):
        assert complete_request(port, stream=stream) == (200, expected)
        assert seen == [*KEYS, KEYS[0]]
        assert delays == [2]


def test_repeated_rounds_back_off_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[int] = []

    def record_delay(_handler: NIMProxyHandler, seconds: int) -> bool:
        delays.append(seconds)
        return True

    monkeypatch.setattr(NIMProxyHandler, "_wait_for_pool_retry_round", record_delay)
    with running_pool(
        lambda handler, i: respond_with(handler, 500 if i < 8 else 200), pool_keys=KEYS[:1]
    ) as (port, seen, _):
        assert complete_request(port)[0] == 200
        assert seen == [KEYS[0]] * 8
    assert delays == [2, 4, 8, 16, 30, 30, 30]


@pytest.mark.parametrize("status", [400, 404, 422, 501])
def test_permanent_errors_are_not_retried(status: int) -> None:
    with running_pool(lambda handler, _: respond_with(handler, status)) as (port, seen, _):
        assert complete_request(port)[0] == status
        assert seen == [KEYS[0]]


def test_all_invalid_keys_stop_without_another_round() -> None:
    with running_pool(lambda handler, _: respond_with(handler, 401)) as (port, seen, config):
        assert complete_request(port)[0] == 401
        assert seen == list(KEYS)
        assert config.key_pool is not None
        assert config.key_pool.snapshot().quarantined == len(KEYS)


def test_last_key_auth_failure_does_not_end_a_round_with_healthy_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(NIMProxyHandler, "_wait_for_pool_retry_round", lambda *_: True)
    statuses = (500, 403, 200)
    with running_pool(
        lambda handler, i: respond_with(handler, statuses[i - 1]), pool_keys=KEYS[:2]
    ) as (port, seen, config):
        assert complete_request(port)[0] == 200
        assert seen == [KEYS[0], KEYS[1], KEYS[0]]
        assert config.key_pool is not None and config.key_pool.snapshot().quarantined == 1


def test_retry_after_can_outlast_normal_queue_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NIMProxyHandler, "_wait_for_pool_retry_round", lambda *_: True)
    sent_at: list[float] = []

    def respond(handler: BaseHTTPRequestHandler, number: int) -> None:
        sent_at.append(time.monotonic())
        respond_with(handler, 503 if number == 1 else 200, retry_after="1")

    with running_pool(respond, pool_keys=KEYS[:1], queue_wait_seconds=0.05) as (port, _, _):
        assert complete_request(port)[0] == 200
    assert sent_at[1] - sent_at[0] >= 1


@pytest.mark.parametrize("cooldown", [False, True])
def test_disconnected_client_releases_loop_capacity(
    monkeypatch: pytest.MonkeyPatch, cooldown: bool
) -> None:
    waiting = Event()
    wait_for_round = NIMProxyHandler._wait_for_pool_retry_round

    def signal_wait(handler: NIMProxyHandler, seconds: int) -> bool:
        waiting.set()
        return cooldown or wait_for_round(handler, seconds)

    monkeypatch.setattr(NIMProxyHandler, "_wait_for_pool_retry_round", signal_wait)

    def respond(handler: BaseHTTPRequestHandler, _: int) -> None:
        respond_with(handler, 429 if cooldown else 500, retry_after="30")

    with running_pool(respond, pool_keys=KEYS[:1], max_total_queued=1) as (port, seen, config):
        pool = config.key_pool
        slots = config.pool_retry_slots
        assert pool is not None and slots is not None
        assert slots.acquire(blocking=False)  # Reserve one of two slots to make saturation observable.
        conn = send_request(port)
        try:
            assert waiting.wait(2)
            assert not slots.acquire(blocking=False)
            status, body = complete_request(port)
            assert status == 429 and b"proxy_queue_full" in body
            if cooldown:
                deadline = time.monotonic() + 1
                while pool.snapshot().queued == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert pool.snapshot().queued == 1
        finally:
            conn.close()
        assert slots.acquire(timeout=1)
        assert pool.snapshot().queued == pool.snapshot().active == 0
        slots.release()
        slots.release()
        assert seen == [KEYS[0]]


def test_client_disconnect_during_attempt_prevents_next_attempt() -> None:
    reached_upstream = Event()
    finish_attempt = Event()

    def respond(handler: BaseHTTPRequestHandler, _: int) -> None:
        reached_upstream.set()
        finish_attempt.wait(2)
        respond_with(handler, 503)

    with running_pool(respond, pool_keys=KEYS[:2], max_total_queued=1) as (port, seen, config):
        slots = config.pool_retry_slots
        assert slots is not None
        assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
        conn = send_request(port)
        try:
            assert reached_upstream.wait(2)
        finally:
            conn.close()
            finish_attempt.set()
        assert slots.acquire(timeout=2)
        assert seen == [KEYS[0]]
        for _ in range(3):
            slots.release()


@pytest.mark.parametrize("error", [TimeoutError, socket.gaierror, ssl.SSLCertVerificationError])
def test_transport_failure_only_retries_non_dns_non_tls(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    open_attempt = NIMProxyHandler._open_upstream_attempt
    tried: list[str] = []

    def flaky_open(handler: NIMProxyHandler, request: UpstreamRequest) -> UpstreamAttempt:
        tried.append(request.headers["Authorization"])
        if len(tried) == 1:
            raise error("simulated transport failure")
        return open_attempt(handler, request)

    monkeypatch.setattr(NIMProxyHandler, "_open_upstream_attempt", flaky_open)
    with running_pool(lambda handler, _: respond_with(handler, 200)) as (port, seen, _):
        if error is TimeoutError:
            assert complete_request(port)[0] == 200
            assert seen == [KEYS[1]]
            assert tried == [f"Bearer {key}" for key in KEYS[:2]]
        else:
            assert complete_request(port)[0] == 502
            assert not seen and len(tried) == 1


def test_truncated_sse_is_never_replayed_after_headers() -> None:
    data = b'data: {"choices": []}\n\n'

    def respond(handler: BaseHTTPRequestHandler, _: int) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        handler.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        # Closing without the terminating chunk makes the upstream read fail.

    with running_pool(respond, tool_call_text_mode="pass") as (port, seen, _):
        assert complete_request(port, stream=True) == (200, data)
        assert seen == [KEYS[0]]
