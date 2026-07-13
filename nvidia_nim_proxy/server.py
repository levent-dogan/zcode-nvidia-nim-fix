"""Local OpenAI-compatible proxy for NVIDIA NIM chat completions."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from nvidia_nim_proxy import __version__
from nvidia_nim_proxy.sanitizer import ProviderContext, sanitize_chat_completion_body


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_UPSTREAM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 300
MAX_REQUEST_BYTES = 10 * 1024 * 1024
STREAM_CHUNK_SIZE = 8192
STREAM_DIAGNOSTIC_SCAN_BYTES = 8 * 1024
STREAM_DIAGNOSTIC_SCAN_EVENTS = 8
HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
TOOL_CALL_LEAK_MARKERS = (b"<tool_call", b"</tool_call", b"&lt;tool_call", b"&lt;/tool_call")
API_KEY_MODE_ENV = "env"
API_KEY_MODE_CLIENT = "client"
TOOL_CALL_TEXT_DIAGNOSTIC = (
    "Provider/model compatibility issue: NVIDIA NIM returned tool-call markup as normal "
    "assistant text instead of real OpenAI-compatible tool_calls. The proxy did not execute "
    "that text. Use this model for normal chat, or switch ZCode to a provider/model that "
    "supports real OpenAI tool calls for agentic file and command workflows."
)

logger = logging.getLogger("zcode-nim-proxy")


class ProxyConfig:
    """Runtime settings for the local proxy."""

    def __init__(
        self,
        upstream_base_url: str,
        api_key: str | None,
        tool_call_text_mode: str = "diagnostic",
        api_key_mode: str = API_KEY_MODE_ENV,
        upstream_timeout_seconds: int = DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    ) -> None:
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.api_key = api_key
        self.tool_call_text_mode = tool_call_text_mode
        self.api_key_mode = api_key_mode
        self.upstream_timeout_seconds = upstream_timeout_seconds


@dataclass(frozen=True)
class UpstreamRequest:
    """Prepared request details for NVIDIA NIM."""

    netloc: str
    path: str
    payload: bytes
    headers: dict[str, str]
    stream: bool
    use_tls: bool
    api_key_fingerprint: str


@dataclass(frozen=True)
class StreamPrefixScan:
    """Bounded stream prefix used to detect tool-call text leaks."""

    buffered: bytes
    tool_call_text_leak: bool
    upstream_exhausted: bool


class NIMProxyServer(ThreadingHTTPServer):
    """Threaded local server that does not block shutdown on active requests."""

    daemon_threads = True
    allow_reuse_address = True


def positive_int(value: str) -> int:
    """Parse a positive integer for argparse with a readable error."""

    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc

    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def valid_port(value: str) -> int:
    """Parse a valid TCP port for argparse."""

    parsed = positive_int(value)
    if parsed > 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


def is_loopback_host(host: str) -> bool:
    """Return true only for explicit localhost or loopback IP bindings."""

    normalized = host.strip().lower()
    if normalized == "localhost":
        return True

    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_bind_security(host: str, *, allow_remote: bool, api_key_mode: str) -> None:
    """Reject unsafe network exposure of the environment-backed API key."""

    if is_loopback_host(host):
        return
    if not allow_remote:
        raise ValueError(
            "refusing non-loopback bind without --allow-remote; use 127.0.0.1 or localhost"
        )
    if api_key_mode == API_KEY_MODE_ENV:
        raise ValueError("remote binding is not allowed with env API key mode; use client mode")


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extract a Bearer token without logging or validating token content."""

    if authorization_header is None:
        return None

    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    token = parts[1].strip()
    return token or None


def resolve_upstream_api_key(config: ProxyConfig, client_authorization: str | None) -> str:
    """Resolve which NVIDIA API key should be used for the upstream request."""

    if config.api_key_mode == API_KEY_MODE_CLIENT:
        client_token = extract_bearer_token(client_authorization)
        if client_token is None:
            raise PermissionError("missing client bearer token")
        return client_token

    if config.api_key is None or config.api_key.strip() == "":
        raise ValueError("missing NVIDIA_API_KEY")
    return config.api_key


def fingerprint_secret(secret: str) -> str:
    """Return a stable debug-safe fingerprint for a secret without exposing it."""

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def build_upstream_chat_request(
    body: dict[str, Any],
    config: ProxyConfig,
    *,
    client_authorization: str | None = None,
) -> UpstreamRequest:
    """Build the debug-safe upstream request from an already sanitized body."""

    upstream = urlparse(config.upstream_base_url)
    if upstream.scheme not in {"http", "https"} or not upstream.netloc:
        raise ValueError("invalid upstream base URL")

    api_key = resolve_upstream_api_key(config, client_authorization)
    stream = body.get("stream") is True
    upstream_path = f"{upstream.path.rstrip('/')}/chat/completions"
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }

    return UpstreamRequest(
        netloc=upstream.netloc,
        path=upstream_path,
        payload=payload,
        headers=headers,
        stream=stream,
        use_tls=upstream.scheme == "https",
        api_key_fingerprint=fingerprint_secret(api_key),
    )


def contains_tool_call_text_leak(payload: bytes) -> bool:
    """Detect plain-text tool-call tags without logging response content."""

    lower_payload = payload.lower()
    return any(marker in lower_payload for marker in TOOL_CALL_LEAK_MARKERS)


def parse_completed_sse_events(payload: bytes) -> list[bytes]:
    """Return complete SSE events using normalized line endings."""

    normalized = payload.replace(b"\r\n", b"\n")
    parts = normalized.split(b"\n\n")
    return [event for event in parts[:-1] if event.strip()]


def contains_tool_call_text_leak_in_sse(payload: bytes) -> bool:
    """Detect a tool-call text marker even when content spans SSE events."""

    content_fragments: list[str] = []
    for event in parse_completed_sse_events(payload):
        for line in event.splitlines():
            if not line.startswith(b"data:"):
                continue
            data = line.removeprefix(b"data:").strip()
            if not data or data == b"[DONE]":
                continue
            try:
                decoded = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(decoded, dict):
                continue
            choices = decoded.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str):
                    content_fragments.append(content)

    return contains_tool_call_text_leak("".join(content_fragments).encode("utf-8"))


def read_stream_prefix_for_tool_call_detection(
    read_chunk: Callable[[int], bytes],
    *,
    max_scan_bytes: int = STREAM_DIAGNOSTIC_SCAN_BYTES,
) -> StreamPrefixScan:
    """Read at most the first SSE event without delaying the full stream."""

    buffered = bytearray()

    while len(buffered) < max_scan_bytes:
        read_size = min(STREAM_CHUNK_SIZE, max_scan_bytes - len(buffered))
        chunk = read_chunk(read_size)
        if not chunk:
            return StreamPrefixScan(
                buffered=bytes(buffered),
                tool_call_text_leak=False,
                upstream_exhausted=True,
            )

        buffered.extend(chunk)
        buffered_bytes = bytes(buffered)
        if contains_tool_call_text_leak(buffered_bytes) or contains_tool_call_text_leak_in_sse(
            buffered_bytes
        ):
            return StreamPrefixScan(
                buffered=bytes(buffered),
                tool_call_text_leak=True,
                upstream_exhausted=False,
            )
        if len(parse_completed_sse_events(buffered_bytes)) >= STREAM_DIAGNOSTIC_SCAN_EVENTS:
            return StreamPrefixScan(
                buffered=bytes(buffered),
                tool_call_text_leak=False,
                upstream_exhausted=False,
            )

    return StreamPrefixScan(
        buffered=bytes(buffered),
        tool_call_text_leak=False,
        upstream_exhausted=False,
    )


def build_tool_call_text_diagnostic_response(*, stream: bool) -> bytes:
    """Build an OpenAI-shaped diagnostic response without including leaked content."""

    created = int(time.time())
    response_id = f"zcode-nim-proxy-diagnostic-{created}"

    if not stream:
        payload = {
            "id": response_id,
            "object": "chat.completion",
            "created": created,
            "model": "nvidia-nim-proxy",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": TOOL_CALL_TEXT_DIAGNOSTIC,
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    first_chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "nvidia-nim-proxy",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": TOOL_CALL_TEXT_DIAGNOSTIC,
                },
                "finish_reason": None,
            }
        ],
    }
    final_chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "nvidia-nim-proxy",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    lines = (
        f"data: {json.dumps(first_chunk, separators=(',', ':'))}\n\n"
        f"data: {json.dumps(final_chunk, separators=(',', ':'))}\n\n"
        "data: [DONE]\n\n"
    )
    return lines.encode("utf-8")


class NIMProxyHandler(BaseHTTPRequestHandler):
    """HTTP handler that accepts OpenAI-compatible requests and forwards to NIM."""

    server_version = f"ZCodeNIMProxy/{__version__}"
    config: ProxyConfig

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "version": __version__,
                    "upstream": self.config.upstream_base_url,
                },
            )
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not_found"})
            return

        try:
            request_body = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        context = ProviderContext(
            provider_name="nvidia-nim",
            provider_code="nim",
            base_url=self.config.upstream_base_url,
        )
        sanitized = sanitize_chat_completion_body(request_body, context)

        if sanitized.stripped_keys:
            logger.info(
                "Stripped unsupported NVIDIA NIM request keys: %s",
                ", ".join(sanitized.stripped_keys),
            )

        logger.debug(
            "Forwarding chat completion request model=%r stream=%r stripped_keys=%s",
            sanitized.body.get("model"),
            sanitized.body.get("stream"),
            sanitized.stripped_keys,
        )

        try:
            self._forward_to_nim(
                sanitized.body,
                client_authorization=self.headers.get("Authorization"),
            )
        except CLIENT_DISCONNECT_ERRORS:
            logger.info("Client disconnected before proxy response completed")

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.client_address[0], fmt % args)

    def _read_json_body(self) -> dict[str, Any]:
        content_length_header = self.headers.get("Content-Length")
        if content_length_header is None:
            raise ValueError("missing Content-Length")

        try:
            content_length = int(content_length_header)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc

        if content_length < 0 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("request body too large")

        raw_body = self.rfile.read(content_length)
        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON request body") from exc

        if not isinstance(decoded, dict):
            raise ValueError("JSON request body must be an object")

        return decoded

    def _forward_to_nim(
        self,
        body: dict[str, Any],
        *,
        client_authorization: str | None,
    ) -> None:
        try:
            upstream_request = build_upstream_chat_request(
                body,
                self.config,
                client_authorization=client_authorization,
            )
        except PermissionError:
            self._send_json(
                401,
                {
                    "error": (
                        "missing client Authorization bearer token; configure an NVIDIA API key "
                        "in the ZCode provider API key field or use --api-key-mode env"
                    )
                },
            )
            return
        except ValueError:
            self._send_json(500, {"error": "invalid upstream base URL"})
            return

        connection_cls = http.client.HTTPSConnection if upstream_request.use_tls else http.client.HTTPConnection
        connection = connection_cls(
            upstream_request.netloc,
            timeout=self.config.upstream_timeout_seconds,
        )
        started_at = time.monotonic()
        try:
            connection.request(
                "POST",
                upstream_request.path,
                body=upstream_request.payload,
                headers=upstream_request.headers,
            )
            upstream_response = connection.getresponse()
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            self._log_upstream_response(
                upstream_response,
                body=body,
                upstream_request=upstream_request,
                elapsed_ms=elapsed_ms,
            )
            self._relay_upstream_response(upstream_response, stream=upstream_request.stream)
        except CLIENT_DISCONNECT_ERRORS:
            raise
        except TimeoutError:
            logger.error(
                (
                    "Timed out while waiting for NVIDIA NIM after %s seconds; "
                    "model=%r stream=%r api_key_fingerprint=%s"
                ),
                self.config.upstream_timeout_seconds,
                body.get("model"),
                body.get("stream"),
                upstream_request.api_key_fingerprint,
            )
            self._send_json(504, {"error": "upstream timeout"})
        except OSError as exc:
            logger.error(
                "Failed to forward request to NVIDIA NIM; model=%r stream=%r api_key_fingerprint=%s error=%s",
                body.get("model"),
                body.get("stream"),
                upstream_request.api_key_fingerprint,
                exc.__class__.__name__,
            )
            self._send_json(502, {"error": "upstream request failed"})
        finally:
            connection.close()

    def _log_upstream_response(
        self,
        upstream_response: http.client.HTTPResponse,
        *,
        body: dict[str, Any],
        upstream_request: UpstreamRequest,
        elapsed_ms: int,
    ) -> None:
        retry_after = upstream_response.getheader("Retry-After")
        message = (
            "NVIDIA NIM upstream response status=%s reason=%r elapsed_ms=%s "
            "model=%r stream=%r api_key_fingerprint=%s retry_after=%r"
        )
        args = (
            upstream_response.status,
            upstream_response.reason,
            elapsed_ms,
            body.get("model"),
            upstream_request.stream,
            upstream_request.api_key_fingerprint,
            retry_after,
        )

        if upstream_response.status == 429 or upstream_response.status >= 500:
            logger.warning(message, *args)
        else:
            logger.debug(message, *args)

    def _relay_upstream_response(
        self,
        upstream_response: http.client.HTTPResponse,
        *,
        stream: bool,
    ) -> None:
        if not stream:
            response_body = upstream_response.read()
            if contains_tool_call_text_leak(response_body):
                logger.warning(
                    "Upstream response contains plain-text tool_call tags. "
                    "This usually means the selected model is not emitting real OpenAI tool_calls."
                )
                if (
                    self.config.tool_call_text_mode == "diagnostic"
                    and 200 <= upstream_response.status < 300
                ):
                    self._send_tool_call_text_diagnostic(stream=False)
                    return
            self.send_response(upstream_response.status, upstream_response.reason)
            for header, value in upstream_response.getheaders():
                if header.lower() not in HOP_BY_HOP_RESPONSE_HEADERS and header.lower() != "content-length":
                    self.send_header(header, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
            return

        if self.config.tool_call_text_mode == "diagnostic" and 200 <= upstream_response.status < 300:
            prefix_scan = read_stream_prefix_for_tool_call_detection(upstream_response.read1)
            if prefix_scan.tool_call_text_leak:
                logger.warning(
                    "Streaming response contains plain-text tool_call tags. "
                    "Returning readable diagnostic response instead of raw tool-call markup."
                )
                self._send_tool_call_text_diagnostic(stream=True)
                return

            self.send_response(upstream_response.status, upstream_response.reason)
            for header, value in upstream_response.getheaders():
                if header.lower() not in HOP_BY_HOP_RESPONSE_HEADERS and header.lower() != "content-length":
                    self.send_header(header, value)
            self.end_headers()
            if prefix_scan.buffered:
                self.wfile.write(prefix_scan.buffered)
                self.wfile.flush()

            if not prefix_scan.upstream_exhausted:
                self._relay_remaining_stream_after_headers(
                    upstream_response,
                    warn_on_tool_call_text=True,
                )

            self.close_connection = True
            return

        self.send_response(upstream_response.status, upstream_response.reason)
        for header, value in upstream_response.getheaders():
            if header.lower() not in HOP_BY_HOP_RESPONSE_HEADERS:
                self.send_header(header, value)
        self.end_headers()

        self._relay_remaining_stream_after_headers(
            upstream_response,
            warn_on_tool_call_text=True,
        )

        self.close_connection = True

    def _relay_remaining_stream(
        self,
        upstream_response: http.client.HTTPResponse,
        *,
        warn_on_tool_call_text: bool = False,
    ) -> None:
        tool_call_warning_logged = False

        while True:
            chunk = upstream_response.read1(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            if (
                warn_on_tool_call_text
                and not tool_call_warning_logged
                and contains_tool_call_text_leak(chunk)
            ):
                logger.warning(
                    "Streaming response contains plain-text tool_call tags. "
                    "This usually means the selected model is not emitting real OpenAI tool_calls."
                )
                tool_call_warning_logged = True
            self.wfile.write(chunk)
            self.wfile.flush()

    def _relay_remaining_stream_after_headers(
        self,
        upstream_response: http.client.HTTPResponse,
        *,
        warn_on_tool_call_text: bool,
    ) -> None:
        """Relay an established stream without attempting a second HTTP status."""

        try:
            self._relay_remaining_stream(
                upstream_response,
                warn_on_tool_call_text=warn_on_tool_call_text,
            )
        except TimeoutError:
            logger.error("NVIDIA NIM stream stalled after the response started; closing connection")
            self.close_connection = True

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        response_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        except CLIENT_DISCONNECT_ERRORS:
            logger.info("Client disconnected before JSON response could be sent; status=%s", status_code)

    def _send_tool_call_text_diagnostic(self, *, stream: bool) -> None:
        response_body = build_tool_call_text_diagnostic_response(stream=stream)
        try:
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache")
            if stream:
                self.send_header("Content-Type", "text/event-stream")
            else:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
            self.wfile.flush()
        except CLIENT_DISCONNECT_ERRORS:
            logger.info("Client disconnected before diagnostic response could be sent")


def build_server(host: str, port: int, config: ProxyConfig) -> NIMProxyServer:
    """Build a configured HTTP server instance."""

    class ConfiguredNIMProxyHandler(NIMProxyHandler):
        pass

    ConfiguredNIMProxyHandler.config = config
    return NIMProxyServer((host, port), ConfiguredNIMProxyHandler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ZCode NVIDIA NIM compatibility proxy.")
    parser.add_argument("--host", default=os.getenv("NIM_PROXY_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=valid_port,
        default=os.getenv("NIM_PROXY_PORT", str(DEFAULT_PORT)),
    )
    parser.add_argument(
        "--upstream-base-url",
        default=os.getenv("NIM_PROXY_UPSTREAM_BASE_URL", DEFAULT_UPSTREAM_BASE_URL),
        help="NVIDIA NIM OpenAI-compatible base URL.",
    )
    parser.add_argument(
        "--tool-call-text-mode",
        choices=("diagnostic", "pass"),
        default=os.getenv("NIM_PROXY_TOOL_CALL_TEXT_MODE", "diagnostic"),
        help="How to handle model output that contains plain-text <tool_call> markup.",
    )
    parser.add_argument(
        "--api-key-mode",
        choices=(API_KEY_MODE_ENV, API_KEY_MODE_CLIENT),
        default=os.getenv("NIM_PROXY_API_KEY_MODE", API_KEY_MODE_ENV),
        help=(
            "Use 'env' to read NVIDIA_API_KEY once, or 'client' to forward the incoming "
            "Authorization bearer token from each ZCode provider."
        ),
    )
    parser.add_argument(
        "--upstream-timeout-seconds",
        type=positive_int,
        default=os.getenv(
            "NIM_PROXY_UPSTREAM_TIMEOUT_SECONDS",
            str(DEFAULT_UPSTREAM_TIMEOUT_SECONDS),
        ),
        help="Seconds to wait for NVIDIA NIM to start responding before returning 504.",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow binding to a non-loopback host. Unsafe with env API key mode.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug-safe request logging.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    api_key = os.getenv("NVIDIA_API_KEY")
    if args.api_key_mode == API_KEY_MODE_ENV and not api_key:
        raise SystemExit("NVIDIA_API_KEY is required")
    try:
        validate_bind_security(
            args.host,
            allow_remote=args.allow_remote,
            api_key_mode=args.api_key_mode,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    config = ProxyConfig(
        upstream_base_url=args.upstream_base_url,
        api_key=api_key,
        tool_call_text_mode=args.tool_call_text_mode,
        api_key_mode=args.api_key_mode,
        upstream_timeout_seconds=args.upstream_timeout_seconds,
    )
    server = build_server(args.host, args.port, config)
    logger.info("Listening on http://%s:%s/v1", args.host, args.port)
    logger.info("Forwarding sanitized requests to %s", args.upstream_base_url)
    logger.info("Plain-text tool_call handling mode: %s", args.tool_call_text_mode)
    logger.info("API key mode: %s", args.api_key_mode)
    logger.info("Upstream timeout: %s seconds", args.upstream_timeout_seconds)
    if not is_loopback_host(args.host):
        logger.warning("Remote client-key proxy binding enabled on %s", args.host)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
