"""Upstream proxy tunnel tests: SOCKS5, HTTP CONNECT, validation, and catalog wiring."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from nvidia_nim_proxy.model_catalog import fetch_nvidia_catalog
from nvidia_nim_proxy.server import ProxyConfig, build_server
from nvidia_nim_proxy.upstream import UpstreamProxy, validate_upstream_proxy


class MockUpstream(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"id": "test", "object": "chat.completion", "choices": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("unexpected EOF during proxy handshake")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def relay(a: socket.socket, b: socket.socket) -> None:
    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    to_target = threading.Thread(target=pump, args=(a, b), daemon=True)
    to_client = threading.Thread(target=pump, args=(b, a), daemon=True)
    to_target.start()
    to_client.start()
    to_target.join(timeout=10)
    to_client.join(timeout=10)
    b.close()


class FakeSocks5Proxy:
    """Minimal no-auth SOCKS5 server that tunnels TCP after a handshake."""

    def __init__(self) -> None:
        self.targets: list[tuple[str, int]] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._sock.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            # VER + NMETHODS + METHOD: exactly 3 bytes, recv(2) would leave one buffered.
            recv_exact(conn, 3)
            conn.sendall(b"\x05\x00")
            header = recv_exact(conn, 4)
            atyp = header[3]
            if atyp == 1:
                host = socket.inet_ntoa(recv_exact(conn, 4))
            elif atyp == 3:
                host = recv_exact(conn, recv_exact(conn, 1)[0]).decode()
            else:
                host = socket.inet_ntop(socket.AF_INET6, recv_exact(conn, 16))
            port = int.from_bytes(recv_exact(conn, 2), "big")
            self.targets.append((host, port))
            conn.sendall(
                b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + (0).to_bytes(2, "big")
            )
            relay(conn, socket.create_connection((host, port), timeout=5))
        except OSError:
            conn.close()


class FakeConnectProxy:
    """Minimal HTTP CONNECT server that tunnels TCP after a 200 response."""

    def __init__(self) -> None:
        self.targets: list[tuple[str, int]] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._sock.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            request = bytearray()
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request.extend(chunk)
            line = request.split(b"\r\n", 1)[0].decode()
            host, port = line.split()[1].rsplit(":", 1)
            self.targets.append((host, int(port)))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            relay(conn, socket.create_connection((host, int(port)), timeout=5))
        except (OSError, IndexError, ValueError):
            conn.close()


@contextmanager
def running_local_proxy(
    upstream_proxy: UpstreamProxy,
) -> Iterator[tuple[int, ThreadingHTTPServer]]:
    with ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream) as upstream:
        upstream_thread = threading.Thread(
            target=upstream.serve_forever, kwargs={"poll_interval": 0.01}
        )
        upstream_thread.start()
        config = ProxyConfig(
            upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
            api_key="env-secret",
            upstream_proxy=upstream_proxy,
        )
        with build_server("127.0.0.1", 0, config) as local:
            thread = threading.Thread(target=local.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                yield local.server_port, upstream
            finally:
                local.shutdown()
            thread.join(timeout=2)
            upstream.shutdown()
            upstream_thread.join(timeout=2)


def post_chat(port: int) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps({"model": "test-model", "messages": []})
    try:
        conn.request(
            "POST",
            "/v1/chat/completions",
            body,
            {"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_chat_request_tunnels_through_socks5_proxy() -> None:
    socks = FakeSocks5Proxy()
    socks.start()
    try:
        with running_local_proxy(
            UpstreamProxy(scheme="socks5", host="127.0.0.1", port=socks.port)
        ) as (port, upstream):
            status, body = post_chat(port)
    finally:
        socks.stop()

    assert status == 200
    assert b"chat.completion" in body
    assert socks.targets == [("127.0.0.1", upstream.server_port)]


def test_chat_request_tunnels_through_http_connect_proxy() -> None:
    connect = FakeConnectProxy()
    connect.start()
    try:
        with running_local_proxy(
            UpstreamProxy(scheme="http", host="127.0.0.1", port=connect.port)
        ) as (port, upstream):
            status, body = post_chat(port)
    finally:
        connect.stop()

    assert status == 200
    assert b"chat.completion" in body
    assert connect.targets == [("127.0.0.1", upstream.server_port)]


def test_unreachable_upstream_proxy_returns_502() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    with running_local_proxy(
        UpstreamProxy(scheme="socks5", host="127.0.0.1", port=dead_port)
    ) as (port, _upstream):
        status, body = post_chat(port)

    assert status == 502
    assert b"upstream_request_failed" in body


@pytest.mark.parametrize("value", [
    "",
    "ftp://127.0.0.1:1080",
    "socks5://127.0.0.1",
    "socks5://127.0.0.1:notaport",
    "socks5://user:pass@127.0.0.1:1080",
    "socks5://127.0.0.1:1080?upstream=1",
    "socks5://127.0.0.1:1080/extra",
])
def test_invalid_upstream_proxy_urls_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        validate_upstream_proxy(value)


def test_valid_upstream_proxy_urls_are_parsed() -> None:
    assert validate_upstream_proxy("socks5://127.0.0.1:10808") == UpstreamProxy(
        scheme="socks5", host="127.0.0.1", port=10808
    )
    assert validate_upstream_proxy("http://localhost:10809") == UpstreamProxy(
        scheme="http", host="localhost", port=10809
    )


def test_catalog_fetch_uses_the_upstream_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {"data": [{"id": "test-model", "object": "model", "created": 1, "owned_by": "nvidia"}]}
    ).encode()
    seen: list[UpstreamProxy] = []

    class FakeResponse:
        status = 200

        def __init__(self, data: bytes) -> None:
            self._data = data

        def read1(self, _size: int) -> bytes:
            chunk, self._data = self._data, b""
            return chunk

    class FakeConnection:
        def __init__(self, _host: str, *, proxy: UpstreamProxy, timeout: float) -> None:
            seen.append(proxy)

        def request(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return FakeResponse(payload)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "nvidia_nim_proxy.model_catalog.UpstreamHTTPSConnection", FakeConnection
    )
    expected = UpstreamProxy(scheme="socks5", host="127.0.0.1", port=10808)
    result = fetch_nvidia_catalog(expected)

    assert seen == [expected]
    assert result["data"][0]["id"] == "test-model"
