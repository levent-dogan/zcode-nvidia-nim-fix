from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

import nvidia_nim_proxy.server as module
from nvidia_nim_proxy.model_catalog import ModelCatalog
from nvidia_nim_proxy.server import NIMProxyHandler, ProxyConfig, build_server


@pytest.mark.parametrize("mode", ["env", "client", "pool"])
def test_models_endpoint_auth_cache_health_and_manual_model_fallback(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = ModelCatalog(tmp_path / "cache", fetcher=lambda: {"data": [{"id": "catalog/model"}]})
    config = ProxyConfig(
        "https://integrate.api.nvidia.com/v1",
        api_key="upstream-secret",
        api_key_mode=mode,
        pool_keys=("upstream-secret",) if mode == "pool" else (),
        local_client_key="local-secret",
        model_catalog=catalog,
    )
    forwarded: list[dict[str, Any]] = []

    def forward(
        handler: NIMProxyHandler, body: dict[str, Any], *, client_authorization: str | None
    ) -> None:
        forwarded.append(body)
        handler._send_json(200, {"ok": True})

    monkeypatch.setattr(NIMProxyHandler, "_forward_to_nim", forward)
    proxy = build_server("127.0.0.1", 0, config)
    thread = Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    def request(
        path: str, token: str | None = None, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=2)
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            connection.request(
                "POST" if body else "GET",
                path,
                body=json.dumps(body) if body else None,
                headers=headers,
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    token = "local-secret" if mode == "pool" else "upstream-secret"
    try:
        status, payload = request("/v1/models", token)
        assert status == 503
        assert payload["error"]["code"] == "model_catalog_unavailable"
        assert catalog.refresh()
        assert request("/v1/models", token) == (200, catalog.payload())
        assert request("/v1/models/", token) == (200, catalog.payload())
        if mode != "env":
            assert request("/v1/models")[0] == 401
        if mode == "pool":
            assert request("/v1/models", "wrong-secret")[0] == 401
        health_status, health = request("/health")
        assert health_status == 200
        assert health["model_catalog"]["count"] == 1
        assert health["model_catalog"]["status"] == "ready"
        assert health["queue"] == {"active": 0, "queued": 0}
        assert "secret" not in json.dumps(health)
        # Discovery is informational: it never gates an explicitly selected model.
        assert (
            request(
                "/v1/chat/completions",
                token,
                {
                    "model": "moonshotai/kimi-k3",
                    "messages": [],
                    "stream": True,
                },
            )[0]
            == 200
        )
        assert forwarded[-1]["reasoning_effort"] == "max"
        assert (
            request(
                "/v1/chat/completions",
                token,
                {
                    "model": "manual/new-model",
                    "messages": [],
                },
            )[0]
            == 200
        )
        assert forwarded[-1] == {"model": "manual/new-model", "messages": []}
        config.model_catalog = None
        assert request("/v1/models", token)[1]["error"]["code"] == "model_catalog_disabled"
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=2)
    assert not catalog.refresh()


@pytest.mark.parametrize(
    "extra_args,enabled",
    [
        ([], True),
        (["--no-model-discovery"], False),
        (["--upstream-base-url", "http://localhost:9999/v1"], False),
    ],
)
def test_main_starts_catalog_only_for_public_upstream(
    extra_args: list[str],
    enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["proxy", "--api-key-mode", "client", *extra_args])
    starts: list[ModelCatalog] = []
    closed: list[bool] = []
    configs: list[ProxyConfig] = []
    monkeypatch.setattr(ModelCatalog, "start", lambda self: starts.append(self))

    class FakeServer:
        def serve_forever(self) -> None:
            raise KeyboardInterrupt()

        def server_close(self) -> None:
            closed.append(True)

    def build(host: str, port: int, config: ProxyConfig) -> Any:
        configs.append(config)
        return FakeServer()

    monkeypatch.setattr(module, "build_server", build)
    module.main()
    assert len(starts) == int(enabled)
    assert (configs[0].model_catalog is not None) == enabled
    assert closed == [True]


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--no-model-discovery"],
        ["--upstream-base-url", "http://localhost:9999/v1"],
        ["--host", "0.0.0.0", "--allow-remote"],
    ],
)
def test_invalid_sync_modes_fail_before_server_start(
    extra_args: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "proxy",
            "--api-key-mode",
            "client",
            "--opencode-config",
            str(tmp_path / "opencode.json"),
            *extra_args,
        ],
    )
    with pytest.raises(SystemExit, match="requires"):
        module.main()
