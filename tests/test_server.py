import argparse
import io
import json
import time
from threading import Event, Thread
from typing import Any, cast

import pytest

import nvidia_nim_proxy.server as server_module
from nvidia_nim_proxy.sanitizer import ProviderContext, sanitize_chat_completion_body
from nvidia_nim_proxy.server import (
    API_KEY_MODE_CLIENT,
    API_KEY_MODE_ENV,
    API_KEY_MODE_POOL,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    NIMProxyServer,
    NIMProxyHandler,
    TOOL_CALL_TEXT_DIAGNOSTIC,
    ProxyConfig,
    UpstreamAttempt,
    UpstreamRequest,
    build_health_payload,
    build_tool_call_text_diagnostic_response,
    build_upstream_chat_request,
    contains_tool_call_text_leak,
    contains_tool_call_text_leak_in_sse,
    extract_bearer_token,
    fingerprint_secret,
    is_loopback_host,
    main,
    non_negative_int,
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


@pytest.mark.parametrize(
    "base_url",
    [
        "not-a-url",
        "https://user:secret@integrate.api.nvidia.com/v1",
        "https://integrate.api.nvidia.com/v1?api_key=secret",
        "https://integrate.api.nvidia.com/v1#secret",
    ],
)
def test_proxy_config_rejects_invalid_or_secret_bearing_base_url(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="upstream base URL") as exc_info:
        build_upstream_chat_request(
            {"model": "z-ai/glm-5.2", "messages": []},
            ProxyConfig(upstream_base_url=base_url, api_key="secret-test-key"),
        )
    assert "secret@" not in str(exc_info.value)
    assert "api_key=secret" not in str(exc_info.value)


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
    validate_bind_security("0.0.0.0", allow_remote=True, api_key_mode=API_KEY_MODE_POOL)


def test_cli_integer_validation() -> None:
    assert positive_int("300") == 300
    assert non_negative_int("0") == 0
    assert valid_port("8787") == 8787

    with pytest.raises(argparse.ArgumentTypeError):
        positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        non_negative_int("-1")
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


def test_secret_fingerprint_is_stable_and_does_not_expose_secret() -> None:
    fingerprint = fingerprint_secret("client-secret")

    assert fingerprint == fingerprint_secret("client-secret")
    assert len(fingerprint) == 12
    assert "client-secret" not in fingerprint


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
    assert request.api_key_fingerprint == fingerprint_secret("client-secret")
    assert request.api_key_fingerprint != "client-secret"


def test_build_upstream_request_pool_override_never_forwards_local_key() -> None:
    request = build_upstream_chat_request(
        {"model": "z-ai/glm-5.2", "messages": []},
        ProxyConfig(
            upstream_base_url="https://integrate.api.nvidia.com/v1",
            api_key=None,
            api_key_mode=API_KEY_MODE_POOL,
            local_client_key="local-proxy-secret",
            pool_keys=("nvidia-key-one",),
        ),
        api_key_override="nvidia-key-one",
    )

    assert request.headers["Authorization"] == "Bearer nvidia-key-one"
    assert "local-proxy-secret" not in request.headers["Authorization"]


def test_main_rejects_local_proxy_key_reused_as_nvidia_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server_module,
        "parse_args",
        lambda: argparse.Namespace(api_key_mode=API_KEY_MODE_POOL, debug=False),
    )
    monkeypatch.setenv("NIM_PROXY_CLIENT_KEY", "shared-secret")
    monkeypatch.setenv("NVIDIA_API_KEY_1", "shared-secret")

    with pytest.raises(
        SystemExit,
        match="NIM_PROXY_CLIENT_KEY must be different",
    ):
        main()


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeResponse:
    def __init__(
        self,
        status: int,
        *,
        retry_after: str | None = None,
    ) -> None:
        self.status = status
        self.reason = "test"
        self._retry_after = retry_after
        self.read_count = 0

    def getheader(self, name: str) -> str | None:
        if name.lower() == "retry-after":
            return self._retry_after
        return None

    def getheaders(self) -> list[tuple[str, str]]:
        return []

    def read(self, _size: int = -1) -> bytes:
        self.read_count += 1
        return b'{"error":"test"}'

    def read1(self, _size: int = -1) -> bytes:
        return b""


def _pool_config(
    *,
    keys: tuple[str, ...] = ("nvidia-key-one", "nvidia-key-two"),
) -> ProxyConfig:
    return ProxyConfig(
        upstream_base_url="https://integrate.api.nvidia.com/v1",
        api_key=None,
        api_key_mode=API_KEY_MODE_POOL,
        local_client_key="local-proxy-secret",
        pool_keys=keys,
        queue_wait_seconds=1,
    )


def _run_fake_pool_forward(
    statuses: list[int],
    *,
    authorization: str = "Bearer local-proxy-secret",
    stream: bool = False,
    relay_failures: int = 0,
) -> tuple[list[UpstreamRequest], list[int], list[tuple[int, str]]]:
    handler = cast(Any, object.__new__(NIMProxyHandler))
    handler.config = _pool_config()
    requests: list[UpstreamRequest] = []
    relayed: list[int] = []
    errors: list[tuple[int, str]] = []
    relay_attempts = 0

    def open_attempt(request: UpstreamRequest) -> UpstreamAttempt:
        requests.append(request)
        response = _FakeResponse(
            statuses[len(requests) - 1],
            retry_after="1",
        )
        return UpstreamAttempt(
            connection=cast(Any, _FakeConnection()),
            response=cast(Any, response),
            request=request,
            elapsed_ms=1,
        )

    def relay(response: _FakeResponse, *, stream: bool) -> None:
        nonlocal relay_attempts
        relay_attempts += 1
        if relay_attempts <= relay_failures:
            raise TimeoutError("upstream body read timed out")
        relayed.append(response.status)

    def send_proxy_error(
        status_code: int,
        *,
        code: str,
        message: str,
        retry_after: str | None = None,
    ) -> None:
        errors.append((status_code, code))

    handler._open_upstream_attempt = open_attempt
    handler._relay_upstream_response = relay
    handler._log_upstream_response = lambda *args, **kwargs: None
    handler._send_proxy_error = send_proxy_error
    handler._forward_to_nim(
        {
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
        },
        client_authorization=authorization,
    )
    return requests, relayed, errors


def test_pool_429_switches_key_without_forwarding_local_secret() -> None:
    requests, relayed, errors = _run_fake_pool_forward([429, 200])

    assert [request.headers["Authorization"] for request in requests] == [
        "Bearer nvidia-key-one",
        "Bearer nvidia-key-two",
    ]
    assert all(
        "local-proxy-secret" not in request.headers["Authorization"]
        for request in requests
    )
    assert relayed == [200]
    assert errors == []


def test_pool_5xx_uses_only_one_alternate_key() -> None:
    requests, relayed, errors = _run_fake_pool_forward([501, 599])

    assert len(requests) == 2
    assert relayed == [599]
    assert errors == []


def test_pool_request_error_does_not_rotate() -> None:
    requests, relayed, errors = _run_fake_pool_forward([400])

    assert len(requests) == 1
    assert relayed == [400]
    assert errors == []


def test_pool_rejects_incorrect_local_key_before_upstream_attempt() -> None:
    requests, relayed, errors = _run_fake_pool_forward(
        [200],
        authorization="Bearer incorrect-local-key",
    )

    assert requests == []
    assert relayed == []
    assert errors == [(401, "invalid_local_proxy_key")]


def test_pool_does_not_retry_after_successful_stream_starts() -> None:
    requests, relayed, errors = _run_fake_pool_forward([200], stream=True)

    assert len(requests) == 1
    assert relayed == [200]
    assert errors == []


def test_pool_retries_one_alternate_when_success_body_read_times_out() -> None:
    requests, relayed, errors = _run_fake_pool_forward(
        [200, 200],
        relay_failures=1,
    )

    assert len(requests) == 2
    assert relayed == [200]
    assert errors == []


def test_direct_mode_maps_upstream_connection_reset_to_502() -> None:
    handler = cast(Any, object.__new__(NIMProxyHandler))
    handler.config = ProxyConfig(
        upstream_base_url="https://integrate.api.nvidia.com/v1",
        api_key="env-secret",
    )
    errors: list[tuple[int, str]] = []

    def fail_open(_request: UpstreamRequest) -> UpstreamAttempt:
        raise ConnectionResetError("upstream reset")

    def send_proxy_error(
        status_code: int,
        *,
        code: str,
        message: str,
        retry_after: str | None = None,
    ) -> None:
        errors.append((status_code, code))

    handler._open_upstream_attempt = fail_open
    handler._send_proxy_error = send_proxy_error
    request = build_upstream_chat_request(
        {"model": "z-ai/glm-5.2", "messages": []},
        handler.config,
    )

    handler._perform_direct_attempt(
        {"model": "z-ai/glm-5.2", "messages": []},
        request,
    )

    assert errors == [(502, "upstream_request_failed")]


def test_transport_error_after_response_start_only_closes_connection() -> None:
    handler = cast(Any, object.__new__(NIMProxyHandler))
    handler._client_response_started = True
    handler.close_connection = False
    handler._send_proxy_error = lambda *args, **kwargs: pytest.fail(
        "a second HTTP response must not be sent"
    )
    request = build_upstream_chat_request(
        {"model": "z-ai/glm-5.2", "messages": [], "stream": True},
        ProxyConfig(
            upstream_base_url="https://integrate.api.nvidia.com/v1",
            api_key="env-secret",
        ),
    )

    handler._handle_relay_transport_error(
        {"model": "z-ai/glm-5.2", "messages": [], "stream": True},
        request,
        TimeoutError("stream stalled"),
    )

    assert handler.close_connection is True


def test_health_payload_contains_counts_without_keys_or_fingerprints() -> None:
    config = _pool_config()
    payload = build_health_payload(config)
    serialized = json.dumps(payload)

    assert payload["mode"] == API_KEY_MODE_POOL
    assert payload["key_pool"]["total"] == 2
    assert payload["key_pool"]["available"] == 2
    assert payload["queue"] == {"active": 0, "queued": 0}
    assert "nvidia-key-one" not in serialized
    assert fingerprint_secret("nvidia-key-one") not in serialized
    assert "local-proxy-secret" not in serialized


def test_client_mode_serializes_same_key_across_handler_threads() -> None:
    config = ProxyConfig(
        upstream_base_url="https://integrate.api.nvidia.com/v1",
        api_key=None,
        api_key_mode=API_KEY_MODE_CLIENT,
        queue_wait_seconds=2,
    )
    first_started = Event()
    release_first = Event()
    order: list[str] = []

    def make_handler(name: str) -> Any:
        handler = cast(Any, object.__new__(NIMProxyHandler))
        handler.config = config
        handler._send_proxy_error = lambda *args, **kwargs: None

        def perform(_body: dict[str, Any], _request: UpstreamRequest) -> None:
            order.append(name)
            if name == "first":
                first_started.set()
                release_first.wait(timeout=2)

        handler._perform_direct_attempt = perform
        return handler

    first_handler = make_handler("first")
    second_handler = make_handler("second")
    body = {"model": "z-ai/glm-5.2", "messages": []}

    first_thread = Thread(
        target=lambda: first_handler._forward_to_nim(
            body,
            client_authorization="Bearer shared-nvidia-key",
        )
    )
    second_thread = Thread(
        target=lambda: second_handler._forward_to_nim(
            body,
            client_authorization="Bearer shared-nvidia-key",
        )
    )
    first_thread.start()
    assert first_started.wait(timeout=2)
    second_thread.start()

    deadline = time.monotonic() + 2
    while config.request_scheduler.snapshot().queued != 1:
        if time.monotonic() >= deadline:
            raise AssertionError("second client request did not enter the queue")
        time.sleep(0.005)

    assert order == ["first"]
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert order == ["first", "second"]
    assert config.request_scheduler.snapshot().active == 0
    assert config.request_scheduler.snapshot().queued == 0


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
