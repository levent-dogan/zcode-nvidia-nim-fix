"""Background, credential-free discovery of the NVIDIA hosted model catalog.

Author: Levent Dogan
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import tempfile
import time
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable


CATALOG_URL = "https://integrate.api.nvidia.com/v1/models"
DEFAULT_REFRESH_SECONDS = 6 * 60 * 60
CATALOG_TIMEOUT_SECONDS = 10
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_MODELS = 10000
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,254}\Z")
logger = logging.getLogger("zcode-nim-proxy")


def cache_directory() -> Path:
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "zcode-nvidia-nim-fix"


def normalize_catalog(payload: Any) -> list[dict[str, Any]]:
    """Reject incomplete snapshots and retain only public OpenAI model metadata."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("invalid model catalog")
    data = payload["data"]
    if not 1 <= len(data) <= MAX_MODELS:
        raise ValueError("empty or oversized model catalog")
    models: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("invalid model catalog entry")
        model_id = item.get("id")
        owner = item.get("owned_by", "nvidia")
        created = item.get("created", 0)
        if (
            not isinstance(model_id, str)
            or _IDENTIFIER.fullmatch(model_id) is None
            or not isinstance(owner, str)
            or _IDENTIFIER.fullmatch(owner) is None
            or type(created) is not int
            or created < 0
            or item.get("object", "model") != "model"
        ):
            raise ValueError("invalid model catalog metadata")
        models[model_id] = {
            "id": model_id,
            "object": "model",
            "created": created,
            "owned_by": owner,
        }
    return [models[key] for key in sorted(models)]


def fetch_nvidia_catalog() -> Any:
    """No keys, redirects, cookies, prompts, or key-pool requests are involved."""
    connection = http.client.HTTPSConnection(
        "integrate.api.nvidia.com", timeout=CATALOG_TIMEOUT_SECONDS
    )
    deadline = time.monotonic() + CATALOG_TIMEOUT_SECONDS
    try:
        connection.request("GET", "/v1/models", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("model catalog HTTP request failed")
        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("model catalog request timed out")
            chunk = response.read1(min(65536, MAX_CATALOG_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_CATALOG_BYTES:
                raise ValueError("oversized model catalog")
        return json.loads(b"".join(chunks))
    finally:
        connection.close()


class ModelCatalog:
    def __init__(
        self,
        cache_path: Path | None = None,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
        fetcher: Callable[[], Any] = fetch_nvidia_catalog,
        on_refresh: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        if refresh_seconds < 30:
            raise ValueError("model refresh interval must be at least 30 seconds")
        self.cache_path = cache_path or cache_directory() / "models.json"
        self.refresh_seconds = refresh_seconds
        self._fetcher = fetcher
        self._on_refresh = on_refresh
        self._lock = Lock()
        self._refresh_lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._models: list[dict[str, Any]] = []
        self._updated_at: float | None = None
        self._failed = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = Thread(target=self._run, name="nim-model-catalog", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=0.2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stale = (
                self._failed
                or self._updated_at is None
                or (time.time() - self._updated_at >= self.refresh_seconds)
            )
            status = (
                ("stale" if stale else "ready")
                if self._models
                else ("unavailable" if self._failed else "loading")
            )
            return {"status": status, "count": len(self._models), "updated_at": self._updated_at}

    def payload(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._models:
                return None
            return {"object": "list", "data": [dict(model) for model in self._models]}

    def load_cache(self) -> None:
        try:
            with self.cache_path.open("rb") as handle:
                raw = handle.read(MAX_CATALOG_BYTES + 1)
            if len(raw) > MAX_CATALOG_BYTES:
                raise ValueError("oversized model cache")
            saved = json.loads(raw)
            if (
                not isinstance(saved, dict)
                or saved.get("version") != 1
                or saved.get("source") != CATALOG_URL
            ):
                raise ValueError("invalid model cache")
            timestamp = saved.get("updated_at")
            if (
                not isinstance(timestamp, (int, float))
                or isinstance(timestamp, bool)
                or not 0 < timestamp <= time.time() + 300
                or not math.isfinite(timestamp)
            ):
                raise ValueError("invalid model cache timestamp")
            models = normalize_catalog(saved)
            with self._lock:
                self._models = models
                self._updated_at = timestamp
            logger.info("Model catalog cache loaded: models=%s", len(models))
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError):
            logger.warning("Model catalog cache unavailable or invalid; awaiting refresh")

    def refresh(self) -> bool:
        # A single worker (or explicit caller) updates a complete snapshot at a time.
        with self._refresh_lock:
            if self._stop.is_set():
                return False
            try:
                models = normalize_catalog(self._fetcher())
            except Exception as exc:
                with self._lock:
                    self._failed = True
                logger.warning(
                    "Model catalog refresh failed: error=%s; retaining last good list",
                    type(exc).__name__,
                )
                return False
            if self._stop.is_set():
                return False
            timestamp = time.time()
            with self._lock:
                self._models = models
                self._updated_at = timestamp
                self._failed = False
            self._save_cache(models, timestamp)
            logger.info("Model catalog refreshed: models=%s", len(models))
            if self._on_refresh is not None and not self._stop.is_set():
                try:
                    self._on_refresh(tuple(model["id"] for model in models))
                except Exception as exc:
                    logger.warning(
                        "OpenCode model sync skipped: error=%s; chat requests are unaffected",
                        type(exc).__name__,
                    )
            return True

    def _save_cache(self, models: list[dict[str, Any]], timestamp: float) -> None:
        temporary: Path | None = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.cache_path.parent,
                prefix=".models-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    {"version": 1, "source": CATALOG_URL, "updated_at": timestamp, "data": models},
                    handle,
                )
            os.replace(temporary, self.cache_path)
        except OSError:
            logger.warning("Model catalog cache could not be saved; using in-memory list")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Model catalog temporary-file cleanup failed")

    def _run(self) -> None:
        self.load_cache()
        failures = 0
        while not self._stop.is_set():
            if self.refresh():
                failures = 0
                delay = self.refresh_seconds
            else:
                failures = min(failures + 1, 5)
                delay = min(self.refresh_seconds, 30 * 2 ** (failures - 1), 300)
            if self._stop.wait(delay):
                break
