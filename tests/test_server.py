import argparse
import io
import json
from typing import Any, cast

import pytest

from nvidia_nim_proxy.sanitizer import ProviderContext, sanitize_chat_completion_body
from nvidia_nim_proxy.server import (
    API_KEY_MODE_CLIENT,
    API_KEY_MODE_ENV,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    NIMProxyServer,
    NIMProxyHandler,
    TOOL_CALL_TEXT_DIAGNOSTIC,
    ProxyConfig,
    build_tool_call_text_diagnostic_response,
    build_upstream_chat_request,
    contains_tool_call_text_leak,
    contains_tool_call_text_leak_in_sse,
    extract_bearer_token,
    is_loopback_host,
    positive_int,
    read_stream_prefix_for_tool_call_detection,
    resolve_upstream_api_key,
    validate_bind_security,
    valid_port,
)


def test_build_upstream_request_uses_sanitized_streaming_payload() -> None:
    body = {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "Say hello."}],
        "stream": True,
        "max_tokens": 128,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    sanitized = sanitize_chat_completion_body(
        body,
        ProviderContext(base_url="https://integrate.api.nvidia.com/v1"),
    )

    request = build_upstream_chat_request(
        sanitized.body,
        ProxyConfig(
            upstream_base_url="https://integrate.api.nvidia.com/v1",
            api_key="secret-test-key",
        ),
    )

    assert request.netloc == "integrate.api.nvidia.com"
    assert request.path == "/v1/chat/completions"
    assert request.stream is True
    assert request.use_tls is True
    assert request.headers["Accept"] == "text/event-stream"
    assert request.headers["Authorization"] == "Bearer secret-test-key"
    assert json.loads(request.payload.decode("utf-8")) == {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "Say hello."}],
        "stream": True,
        "max_tokens": 128,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
    }


def test_build_upstream_request_rejects_invalid_base_url() -> None:
    with pytest.raises(ValueError, match="invalid upstream base URL"):
        build_upstream_chat_request(
            {"model": "z-ai/glm-5.2", "messages": []},
            ProxyConfig(upstream_base_url="not-a-url", api_key="secret-test-key"),
        )


def test_proxy_config_defaults_to_extended_upstream_timeout() -> None:
    config = ProxyConfig(
        upstream_base_url="https://integrate.api.nvidia.com/v1",
        api_key="env-secret",
    )

    assert config.upstream_timeout_seconds == DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    assert config.upstream_timeout_seconds == 300


def test_proxy_config_accepts_custom_upstream_timeout() -> None:
    config = ProxyConfig(
        upstream_base_url="https://integrate.api.nvidia.com/v1",
        api_key="env-secret",
        upstream_timeout_seconds=600,
    )

    assert config.upstream_timeout_seconds == 600


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_host_detection_accepts_local_bindings(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "proxy.internal", ""])
def test_loopback_host_detection_rejects_remote_bindings(host: str) -> None:
    assert is_loopback_host(host) is False


def test_remote_bind_requires_explicit_client_key_mode() -> None:
    with pytest.raises(ValueError, match="without --allow-remote"):
        validate_bind_security("0.0.0.0", allow_remote=False, api_key_mode=API_KEY_MODE_CLIENT)

    with pytest.raises(ValueError, match="not allowed with env API key mode"):
        validate_bind_security("0.0.0.0", allow_remote=True, api_key_mode=API_KEY_MODE_ENV)

    validate_bind_security("0.0.0.0", allow_remote=True, api_key_mode=API_KEY_MODE_CLIENT)


def test_cli_integer_validation() -> None:
    assert positive_int("300") == 300
    assert valid_port("8787") == 8787

    with pytest.raises(argparse.ArgumentTypeError):
        positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        valid_port("70000")


def test_extracts_client_bearer_token_without_logging_key() -> None:
    assert extract_bearer_token("Bearer dummy-token") == "dummy-token"
    assert extract_bearer_token("bearer dummy-token") == "dummy-token"
    assert extract_bearer_token("Basic abc") is None
    assert extract_bearer_token(None) is None


def test_env_api_key_mode_uses_environment_key_from_config() -> None:
    config = ProxyConfig(
        upstream_base_url="https://integrate.api.nvidia.com/v1",
        api_key="env-secret",
        api_key_mode=API_KEY_MODE_ENV,
    )

    assert resolve_upstream_api_key(config, "Bearer client-secret") == "env-secret"


def test_client_api_key_mode_uses_zcode_provider_key() -> None:
    config = ProxyConfig(
        upstream_base_url="https://integrate.api.nvidia.com/v1",
        api_key=None,
        api_key_mode=API_KEY_MODE_CLIENT,
    )

    assert resolve_upstream_api_key(config, "Bearer client-secret") == "client-secret"


def test_client_api_key_mode_requires_incoming_bearer_token() -> None:
    config = ProxyConfig(
        upstream_base_url="https://integrate.api.nvidia.com/v1",
        api_key=None,
        api_key_mode=API_KEY_MODE_CLIENT,
    )

    with pytest.raises(PermissionError, match="missing client bearer token"):
        resolve_upstream_api_key(config, None)


def test_build_upstream_request_can_forward_client_api_key() -> None:
    request = build_upstream_chat_request(
        {"model": "z-ai/glm-5.2", "messages": []},
        ProxyConfig(
            upstream_base_url="https://integrate.api.nvidia.com/v1",
            api_key=None,
            api_key_mode=API_KEY_MODE_CLIENT,
        ),
        client_authorization="Bearer client-secret",
    )

    assert request.headers["Authorization"] == "Bearer client-secret"


def test_detects_plain_text_tool_call_leak_without_parsing_content() -> None:
    payload = b'{"choices":[{"delta":{"content":"<tool_call>Read</tool_call>"}}]}'

    assert contains_tool_call_text_leak(payload) is True


def test_does_not_flag_normal_chat_response_as_tool_call_leak() -> None:
    payload = b'{"choices":[{"message":{"content":"Hello from NVIDIA NIM."}}]}'

    assert contains_tool_call_text_leak(payload) is False


def test_builds_readable_non_stream_tool_call_diagnostic_response() -> None:
    response = json.loads(build_tool_call_text_diagnostic_response(stream=False).decode("utf-8"))

    assert response["object"] == "chat.completion"
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert response["choices"][0]["message"]["content"] == TOOL_CALL_TEXT_DIAGNOSTIC
    assert "<tool_call>" not in response["choices"][0]["message"]["content"]


def test_builds_readable_stream_tool_call_diagnostic_response() -> None:
    response = build_tool_call_text_diagnostic_response(stream=True).decode("utf-8")

    assert response.startswith("data: ")
    assert TOOL_CALL_TEXT_DIAGNOSTIC in response
    assert "data: [DONE]" in response
    assert "<tool_call>" not in response


def test_send_json_suppresses_client_disconnect() -> None:
    class DisconnectedHandler:
        def send_response(self, _status_code: int) -> None:
            raise ConnectionAbortedError("client closed")

        def send_header(self, _name: str, _value: str) -> None:
            raise AssertionError("send_header should not be reached")

        def end_headers(self) -> None:
            raise AssertionError("end_headers should not be reached")

    NIMProxyHandler._send_json(
        cast(Any, DisconnectedHandler()),
        504,
        {"error": "upstream timeout"},
    )


def test_send_diagnostic_suppresses_client_disconnect() -> None:
    class DisconnectedHandler:
        def send_response(self, _status_code: int) -> None:
            raise ConnectionAbortedError("client closed")

        def send_header(self, _name: str, _value: str) -> None:
            raise AssertionError("send_header should not be reached")

        def end_headers(self) -> None:
            raise AssertionError("end_headers should not be reached")

    NIMProxyHandler._send_tool_call_text_diagnostic(
        cast(Any, DisconnectedHandler()),
        stream=True,
    )


def test_stream_prefix_scan_detects_tool_call_text_leak() -> None:
    chunks = [b'data: {"choices":[{"delta":{"content":"<tool_call>"}}]}\n\n']

    def read_chunk(_size: int) -> bytes:
        return chunks.pop(0) if chunks else b""

    scan = read_stream_prefix_for_tool_call_detection(read_chunk)

    assert scan.tool_call_text_leak is True
    assert scan.upstream_exhausted is False


def test_stream_prefix_scan_stops_after_bounded_sse_events() -> None:
    chunks = [
        f'data: {{"choices":[],"index":{index}}}\n\n'.encode("utf-8")
        for index in range(9)
    ]

    def read_chunk(_size: int) -> bytes:
        return chunks.pop(0) if chunks else b""

    scan = read_stream_prefix_for_tool_call_detection(read_chunk)

    assert scan.buffered.count(b"data:") == 8
    assert scan.tool_call_text_leak is False
    assert scan.upstream_exhausted is False
    assert len(chunks) == 1


def test_sse_tool_call_detection_joins_content_fragments() -> None:
    payload = (
        b'data: {"choices":[{"delta":{"content":"<tool"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"_call>"}}]}\n\n'
    )

    assert contains_tool_call_text_leak(payload) is False
    assert contains_tool_call_text_leak_in_sse(payload) is True


def test_stream_prefix_scan_stops_at_limit_for_normal_stream() -> None:
    chunks = [b"a" * 10, b"b" * 10, b"c" * 10]
    read_sizes = []

    def read_chunk(size: int) -> bytes:
        read_sizes.append(size)
        if not chunks:
            return b""
        chunk = chunks.pop(0)
        if len(chunk) <= size:
            return chunk
        chunks.insert(0, chunk[size:])
        return chunk[:size]

    scan = read_stream_prefix_for_tool_call_detection(read_chunk, max_scan_bytes=20)

    assert scan.tool_call_text_leak is False
    assert scan.upstream_exhausted is False
    assert scan.buffered == b"a" * 10 + b"b" * 10
    assert chunks == [b"c" * 10]
    assert read_sizes == [20, 10]


def test_stream_prefix_scan_reports_exhausted_for_short_normal_stream() -> None:
    chunks = [b"data: hello"]

    def read_chunk(_size: int) -> bytes:
        return chunks.pop(0) if chunks else b""

    scan = read_stream_prefix_for_tool_call_detection(read_chunk, max_scan_bytes=20)

    assert scan.tool_call_text_leak is False
    assert scan.upstream_exhausted is True
    assert scan.buffered == b"data: hello"


def test_stream_relay_uses_incremental_read1() -> None:
    class IncrementalResponse:
        def __init__(self) -> None:
            self.chunks = [b"data: first\n\n", b"data: [DONE]\n\n", b""]

        def read1(self, _size: int) -> bytes:
            return self.chunks.pop(0)

        def read(self, _size: int) -> bytes:
            raise AssertionError("stream relay must use read1")

    class RelayHandler:
        def __init__(self) -> None:
            self.wfile = io.BytesIO()

    handler = RelayHandler()
    response = IncrementalResponse()

    NIMProxyHandler._relay_remaining_stream(
        cast(Any, handler),
        cast(Any, response),
    )

    assert handler.wfile.getvalue() == b"data: first\n\ndata: [DONE]\n\n"


def test_threaded_server_uses_daemon_request_threads() -> None:
    assert NIMProxyServer.daemon_threads is True
    assert NIMProxyServer.allow_reuse_address is True
