"""Opt-in synchronization of NVIDIA chat models in OpenCode GUI providers.

Author: Levent Dogan
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from nvidia_nim_proxy.model_catalog import cache_directory
from nvidia_nim_proxy.model_limits import LEGACY_MODEL_LIMIT, MODEL_CONTEXT_LIMITS, model_limits

PROVIDER_ID = "nim-local"
DIRECT_PROVIDER_IDS = tuple(f"nvidia_nim_{index}" for index in range(1, 7))
DIRECT_BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_CONFIG_BYTES = 2 * 1024 * 1024
logger = logging.getLogger("zcode-nim-proxy")

# The public /v1/models feed also includes embeddings, safety classifiers and
# non-chat endpoints. Only sync IDs with a documented chat-completions use case.
OPENCODE_CHAT_MODELS = frozenset(MODEL_CONTEXT_LIMITS)


class OpenCodeSyncError(ValueError):
    """A safe, fixed diagnostic that contains no configuration values."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OpenCodeSyncError("duplicate JSON properties; configuration was not changed")
        result[key] = value
    return result


def _read_config(path: Path) -> bytes | None:
    if path.is_symlink():
        raise OpenCodeSyncError("symbolic-link configuration targets are not supported")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_CONFIG_BYTES:
        raise OpenCodeSyncError("configuration exceeds the size limit")
    return raw


def _merge_provider_models(
    provider: Any, provider_id: str, base_url: str, eligible: list[str]
) -> int:
    if not isinstance(provider, dict):
        raise OpenCodeSyncError(f"{provider_id} provider configuration is not an object")
    options = provider.get("options", {})
    if (
        provider.get("npm") != "@ai-sdk/openai-compatible"
        or not isinstance(options, dict)
        or options.get("baseURL") != base_url
    ):
        raise OpenCodeSyncError(f"{provider_id} belongs to a different endpoint or API adapter")
    models = provider.setdefault("models", {})
    if not isinstance(models, dict):
        raise OpenCodeSyncError(f"{provider_id} model configuration is not an object")
    changed = 0
    for model in list(models):
        if model.startswith("deepseek-ai/") and model != "deepseek-ai/deepseek-v4.1-flash":
            del models[model]
            changed += 1
    for model in eligible:
        limits = model_limits(model)
        if model in models:
            settings = models[model]
            if not isinstance(settings, dict):
                raise OpenCodeSyncError(f"{provider_id} model settings are not an object")
            # Migrate only absent limits or our exact old generated template.
            # Explicit input caps and non-template manual limits remain untouched.
            if "limit" not in settings or settings["limit"] == LEGACY_MODEL_LIMIT:
                if settings.get("limit") != limits:
                    settings["limit"] = limits
                    changed += 1
            continue
        models[model] = {
            "name": model,
            "reasoning": True,
            "tool_call": True,
            "interleaved": {"field": "reasoning_content"},
            "limit": limits,
        }
        changed += 1
    return changed


def merge_models(
    config: dict[str, Any], model_ids: tuple[str, ...], base_url: str
) -> tuple[dict[str, Any], int]:
    """Reconcile known NVIDIA chat models without touching credentials or manual non-DeepSeek IDs."""
    eligible = sorted(set(model_ids) & OPENCODE_CHAT_MODELS)
    result = deepcopy(config)
    if not eligible:
        return result, 0
    providers = result.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise OpenCodeSyncError("provider configuration is not an object")
    provider = providers.setdefault(
        PROVIDER_ID,
        {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Local NVIDIA NIM Proxy",
            "options": {"baseURL": base_url},
            "models": {},
        },
    )
    changed = _merge_provider_models(provider, PROVIDER_ID, base_url, eligible)
    for provider_id in DIRECT_PROVIDER_IDS:
        if provider_id in providers:
            changed += _merge_provider_models(
                providers[provider_id], provider_id, DIRECT_BASE_URL, eligible
            )
    return result, changed


class OpenCodeModelSync:
    def __init__(self, path: Path, base_url: str, backup_directory: Path | None = None) -> None:
        if path.suffix.lower() != ".json":
            raise OpenCodeSyncError("OpenCode sync requires a strict .json file, not JSONC")
        self.path = path.absolute()
        self.base_url = base_url
        self.backup_directory = backup_directory or cache_directory() / "opencode-backups"

    def __call__(self, model_ids: tuple[str, ...]) -> None:
        try:
            changed = self.sync(model_ids)
            logger.info("OpenCode model sync complete: changed=%s; credentials preserved", changed)
        except OpenCodeSyncError as exc:
            logger.warning("OpenCode model sync skipped: %s", exc)
        except (OSError, ValueError, RecursionError):
            logger.warning(
                "OpenCode model sync skipped: file unavailable or invalid; configuration preserved"
            )

    def sync(self, model_ids: tuple[str, ...]) -> int:
        original = _read_config(self.path)
        try:
            config = (
                json.loads(original.decode("utf-8-sig"), object_pairs_hook=_unique_object)
                if original is not None
                else {}
            )
        except (ValueError, UnicodeError) as exc:
            raise OpenCodeSyncError("strict JSON required; configuration was not changed") from exc
        if not isinstance(config, dict):
            raise OpenCodeSyncError("configuration root is not an object")
        updated, changed = merge_models(config, model_ids, self.base_url)
        if not changed:
            return 0
        serialized = (
            json.dumps(updated, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        ).encode("utf-8")
        if len(serialized) > MAX_CONFIG_BYTES:
            raise OpenCodeSyncError("updated configuration would exceed the size limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.path.parent, prefix=".nim-sync-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            if original is None:
                # Atomic, exclusive publication: never replace a concurrently created file.
                os.link(temporary, self.path)
            else:
                # Backups may contain pre-existing user credentials, so keep them outside
                # the repository and never print their contents or upload them.
                self.backup_directory.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self.backup_directory,
                    prefix="opencode-",
                    suffix=".bak",
                    delete=False,
                ) as backup:
                    backup.write(original)
                if _read_config(self.path) != original:
                    raise OpenCodeSyncError(
                        "configuration changed during sync; retry on a later refresh"
                    )
                os.replace(temporary, self.path)
            return changed
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("OpenCode sync temporary-file cleanup failed")
