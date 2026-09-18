from __future__ import annotations

import json
import http.client
import logging
import time
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

import nvidia_nim_proxy.model_catalog as module
from nvidia_nim_proxy.model_catalog import (
    CATALOG_URL,
    ModelCatalog,
    fetch_nvidia_catalog,
    normalize_catalog,
)


def catalog_payload(*ids: str) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "nvidia"} for model in ids
        ],
    }


def test_catalog_normalizes_metadata_and_returns_defensive_copies(tmp_path: Path) -> None:
    payload = catalog_payload("z-ai/glm-5.3", "moonshotai/kimi-k3", "z-ai/glm-5.3")
    payload["data"][0]["api_key"] = "private-secret"
    catalog = ModelCatalog(tmp_path / "models.json", fetcher=lambda: payload)
    assert catalog.payload() is None
    assert catalog.snapshot()["status"] == "loading"
    assert catalog.refresh()
    result = catalog.payload()
    assert result == catalog_payload("moonshotai/kimi-k3", "z-ai/glm-5.3")
    assert result is not None
    result["data"][0]["id"] = "changed"
    assert catalog.payload() != result
    assert catalog.snapshot()["status"] == "ready"
    assert "private-secret" not in catalog.cache_path.read_text()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"data": []},
        {"data": "invalid"},
        {"data": [None]},
        {"data": [{"id": "bad\nmodel"}]},
        {"data": [{"id": "valid", "created": True}]},
        {"data": [{"id": "valid", "owned_by": {}}]},
        {"data": [{"id": "valid", "object": "secret"}]},
        {"data": [{"id": "valid"}, {"id": "bad model"}]},
    ],
)
def test_invalid_catalog_does_not_replace_last_good_snapshot(tmp_path: Path, payload: Any) -> None:
    upstream = catalog_payload("z-ai/glm-5.3")
    catalog = ModelCatalog(tmp_path / "models.json", fetcher=lambda: upstream)
    assert catalog.refresh()
    before = catalog.cache_path.read_bytes()
    upstream = payload
    assert not catalog.refresh()
    assert catalog.cache_path.read_bytes() == before
    assert catalog.payload() == catalog_payload("z-ai/glm-5.3")
    assert catalog.snapshot()["status"] == "stale"


def test_offline_restart_loads_cache_without_sync_or_secret_logging(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "models.json"
    assert ModelCatalog(path, fetcher=lambda: catalog_payload("known/model")).refresh()

    def offline() -> Any:
        raise OSError("secret request headers and content")

    sync_calls: list[tuple[str, ...]] = []
    restarted = ModelCatalog(path, fetcher=offline, on_refresh=sync_calls.append)
    restarted.load_cache()
    assert not restarted.refresh()
    assert restarted.payload() == catalog_payload("known/model")
    assert restarted.snapshot()["status"] == "stale"
    assert sync_calls == []
    assert "secret request" not in caplog.text
    assert "OSError" in caplog.text


@pytest.mark.parametrize("timestamp", [None, True, -1, float("inf"), float("nan"), 10**1000])
def test_invalid_cache_timestamp_is_ignored(tmp_path: Path, timestamp: Any) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": CATALOG_URL,
                "updated_at": timestamp,
                **catalog_payload("known/model"),
            }
        )
    )
    catalog = ModelCatalog(path)
    catalog.load_cache()
    assert catalog.payload() is None


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"[]", b"{}"])
def test_bad_cache_is_not_fatal(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "models.json"
    path.write_bytes(raw)
    catalog = ModelCatalog(path, fetcher=lambda: catalog_payload("known/model"))
    catalog.load_cache()
    assert catalog.refresh()


def test_cache_is_bound_to_source(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": "https://other/v1/models",
                "updated_at": time.time(),
                **catalog_payload("other/model"),
            }
        )
    )
    catalog = ModelCatalog(path)
    catalog.load_cache()
    assert catalog.payload() is None


def test_cache_write_failure_does_not_lose_in_memory_list(tmp_path: Path) -> None:
    path = tmp_path / "directory"
    path.mkdir()
    catalog = ModelCatalog(path, fetcher=lambda: catalog_payload("known/model"))
    assert catalog.refresh()
    assert catalog.payload() is not None
    assert not list(tmp_path.glob(".models-*.tmp"))


def test_refresh_callback_failure_does_not_break_catalog(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def bad_callback(ids: tuple[str, ...]) -> None:
        raise ValueError("private-config")

    catalog = ModelCatalog(
        tmp_path / "cache", fetcher=lambda: catalog_payload("known/model"), on_refresh=bad_callback
    )
    assert catalog.refresh()
    assert catalog.snapshot()["status"] == "ready"
    assert "private-config" not in caplog.text


def test_start_is_nonblocking_and_stop_prevents_late_publication(tmp_path: Path) -> None:
    started, release, finished = Event(), Event(), Event()

    def fetch() -> Any:
        started.set()
        release.wait(3)
        finished.set()
        return catalog_payload("known/model")

    calls: list[tuple[str, ...]] = []
    catalog = ModelCatalog(tmp_path / "cache", fetcher=fetch, on_refresh=calls.append)
    try:
        catalog.start()
        assert started.wait(1)
        catalog.start()
        assert catalog.payload() is None
        catalog.stop()
        release.set()
        assert finished.wait(1)
        catalog.stop()
        assert catalog.payload() is None
        assert calls == []
    finally:
        release.set()
        catalog.stop()


def test_worker_retries_with_backoff_then_resumes_refresh_interval(tmp_path: Path) -> None:
    waits: list[int] = []
    calls = 0

    class FakeStop:
        def is_set(self) -> bool:
            return len(waits) == 4

        def wait(self, delay: int) -> bool:
            waits.append(delay)
            return self.is_set()

    def fetch() -> Any:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError()
        return catalog_payload("known/model")

    catalog = ModelCatalog(tmp_path / "cache", refresh_seconds=21600, fetcher=fetch)
    catalog._stop = cast(Any, FakeStop())
    catalog._run()
    assert waits == [30, 60, 21600, 21600]


@pytest.mark.parametrize("interval", [0, 1, 29])
def test_refresh_interval_is_bounded(tmp_path: Path, interval: int) -> None:
    with pytest.raises(ValueError):
        ModelCatalog(tmp_path / "cache", refresh_seconds=interval)


def test_fetcher_uses_public_endpoint_no_auth_and_bounded_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []
    closed: list[bool] = []

    class Response:
        status = 200
        body = json.dumps(catalog_payload("known/model")).encode()

        def read1(self, size: int) -> bytes:
            chunk, self.body = self.body[: min(size, 7)], self.body[min(size, 7) :]
            return chunk

    class Connection:
        def __init__(self, host: str, timeout: int) -> None:
            assert host == "integrate.api.nvidia.com"
            assert timeout == 10

        def request(self, *args: Any, **kwargs: Any) -> None:
            requests.append((args, kwargs))

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    assert fetch_nvidia_catalog() == catalog_payload("known/model")
    assert requests == [(("GET", "/v1/models"), {"headers": {"Accept": "application/json"}})]
    assert closed == [True]
    monkeypatch.setattr(Response, "status", 302)
    with pytest.raises(ValueError):
        fetch_nvidia_catalog()
    assert closed == [True, True]
    monkeypatch.setattr(Response, "status", 200)
    monkeypatch.setattr(module, "MAX_CATALOG_BYTES", 10)
    with pytest.raises(ValueError, match="oversized"):
        fetch_nvidia_catalog()


def test_catalog_size_and_cache_size_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "MAX_MODELS", 1)
    with pytest.raises(ValueError):
        normalize_catalog(catalog_payload("one", "two"))
    monkeypatch.setattr(module, "MAX_CATALOG_BYTES", 10)
    path = tmp_path / "cache"
    path.write_bytes(b"x" * 11)
    catalog = ModelCatalog(path)
    catalog.load_cache()
    assert catalog.payload() is None


def test_public_metadata_only_in_debug_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = catalog_payload("known/model")
    payload["secret"] = "private-key"
    payload["data"][0]["messages"] = "private-prompt"
    catalog = ModelCatalog(tmp_path / "cache", fetcher=lambda: payload)
    assert catalog.refresh()
    assert "private-" not in caplog.text
