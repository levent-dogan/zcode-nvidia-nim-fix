"""Upstream connection helpers: optional tunneling through a local proxy."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class UpstreamProxy:
    """A validated socks5:// or http:// proxy endpoint, without credentials."""

    scheme: str
    host: str
    port: int


def validate_upstream_proxy(value: str) -> UpstreamProxy:
    """Return a safe parsed proxy endpoint or reject secret-bearing components."""

    proxy = urlparse(value)
    try:
        port = proxy.port
    except ValueError as exc:
        raise ValueError("invalid upstream proxy URL") from exc

    if (
        proxy.scheme not in {"socks5", "http"}
        or proxy.hostname is None
        or port is None
    ):
        raise ValueError(
            "upstream proxy URL must be socks5://host:port or http://host:port"
        )
    if (
        proxy.username is not None
        or proxy.password is not None
        or proxy.params
        or proxy.path not in ("", "/")
        or proxy.query
        or proxy.fragment
    ):
        raise ValueError(
            "upstream proxy URL must not contain credentials, parameters, path, query, or fragment"
        )
    return UpstreamProxy(scheme=proxy.scheme, host=proxy.hostname, port=port)


class UpstreamHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP connection whose socket is opened through an upstream proxy."""

    def __init__(
        self,
        host: str,
        *,
        proxy: UpstreamProxy,
        timeout: float,
    ) -> None:
        super().__init__(host, timeout=timeout)
        self._proxy = proxy

    def connect(self) -> None:
        self.sock = open_proxy_tunnel(self._proxy, self.host, self.port, self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class UpstreamHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection whose TCP socket is opened through an upstream proxy."""

    def __init__(
        self,
        host: str,
        *,
        proxy: UpstreamProxy,
        timeout: float,
    ) -> None:
        super().__init__(host, timeout=timeout)
        self._proxy = proxy

    def connect(self) -> None:
        self.sock = open_proxy_tunnel(self._proxy, self.host, self.port, self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Same default verification policy http.client would use on a direct connection.
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(self.sock, server_hostname=self.host)


def open_proxy_tunnel(
    proxy: UpstreamProxy,
    target_host: str,
    target_port: int,
    timeout: float | None,
) -> socket.socket:
    """Open a raw TCP socket to target_host:target_port through the proxy.

    SOCKS5 sends the hostname so the proxy resolves DNS remotely.
    """

    sock = socket.create_connection((proxy.host, proxy.port), timeout)
    sock.settimeout(timeout)
    try:
        if proxy.scheme == "socks5":
            _socks5_negotiate(sock, target_host, target_port)
        else:
            _http_connect(sock, target_host, target_port)
    except BaseException:
        sock.close()
        raise
    return sock


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("proxy closed the connection mid-handshake")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


_SOCKS5_REPLIES = {
    1: "general failure",
    2: "connection not allowed",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


def _socks5_negotiate(sock: socket.socket, target_host: str, target_port: int) -> None:
    # Greeting: VER 5, one method offered, no-auth.
    sock.sendall(b"\x05\x01\x00")
    reply = _recv_exact(sock, 2)
    if reply[0] != 0x05 or reply[1] != 0x00:
        raise OSError("SOCKS5 proxy rejected the no-auth handshake")

    # Request: VER 5, CMD 1 (CONNECT), RSV 0. Same bytes as the greeting, different meaning.
    sock.sendall(b"\x05\x01\x00" + _socks5_target_bytes(target_host, target_port))
    reply = _recv_exact(sock, 4)
    if reply[1] != 0x00:
        reason = _SOCKS5_REPLIES.get(reply[1], f"error code {reply[1]}")
        raise OSError(f"SOCKS5 proxy connect failed: {reason}")

    # BND.ADDR and BND.PORT must be consumed before the data stream starts.
    atyp = reply[3]
    if atyp == 0x01:
        _recv_exact(sock, 4)
    elif atyp == 0x03:
        _recv_exact(sock, _recv_exact(sock, 1)[0])
    elif atyp == 0x04:
        _recv_exact(sock, 16)
    else:
        raise OSError("SOCKS5 proxy returned an unknown address type")
    _recv_exact(sock, 2)


def _socks5_target_bytes(target_host: str, target_port: int) -> bytes:
    # ATYP: 1 = IPv4, 3 = domain name, 4 = IPv6 (RFC 1928).
    port_bytes = target_port.to_bytes(2, "big")
    try:
        address = ipaddress.ip_address(target_host)
    except ValueError:
        # The domain field is ASCII on the wire; punycode non-ASCII names.
        host = (
            target_host.encode("idna")
            if not target_host.isascii()
            else target_host.encode("ascii")
        )
        return bytes([0x03, len(host)]) + host + port_bytes
    if address.version == 4:
        return bytes([0x01]) + address.packed + port_bytes
    return bytes([0x04]) + address.packed + port_bytes


def _http_connect(sock: socket.socket, target_host: str, target_port: int) -> None:
    request = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n\r\n"
    )
    sock.sendall(request.encode("ascii"))

    # A CONNECT response is headers only; anything this large means a broken proxy.
    response = bytearray()
    while b"\r\n\r\n" not in response:
        if len(response) > 8192:
            raise OSError("HTTP proxy sent an oversized CONNECT response")
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("HTTP proxy closed the connection mid-handshake")
        response.extend(chunk)

    status = response.split(b"\r\n", 1)[0]
    try:
        code = int(status.split()[1])
    except (IndexError, ValueError) as exc:
        raise OSError(
            f"HTTP proxy sent an unparseable CONNECT response: {status!r}"
        ) from exc
    if code != 200:
        raise OSError(f"HTTP proxy CONNECT failed with status {code}")
