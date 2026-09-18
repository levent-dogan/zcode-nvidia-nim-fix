"""Opt-in, add-only synchronization of the proxy's OpenCode GUI provider.

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
from nvidia_nim_proxy.model_profiles import NVIDIA_REASONING_PROFILES


PROVIDER_ID = "nim-local"
MAX_CONFIG_BYTES = 2 * 1024 * 1024
logger = logging.getLogger("zcode-nim-proxy")


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


def merge_models(
    config: dict[str, Any], model_ids: tuple[str, ...], base_url: str
) -> tuple[dict[str, Any], int]:
    """Preserve manual settings and never guess capabilities from a catalog name."""
    eligible = sorted(
        {
            model
            for model in model_ids
            if model in NVIDIA_REASONING_PROFILES
            and NVIDIA_REASONING_PROFILES[model].default == "max"
        }
    )
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
    if not isinstance(provider, dict):
        raise OpenCodeSyncError("nim-local provider configuration is not an object")
    options = provider.get("options", {})
    if (
        provider.get("npm") != "@ai-sdk/openai-compatible"
        or not isinstance(options, dict)
        or options.get("baseURL") != base_url
    ):
        raise OpenCodeSyncError("nim-local belongs to a different endpoint or API adapter")
    models = provider.setdefault("models", {})
    if not isinstance(models, dict):
        raise OpenCodeSyncError("nim-local model configuration is not an object")
    added = 0
    for model in eligible:
        if model in models:
            continue
        models[model] = {
            "name": model,
            "reasoning": True,
            "tool_call": True,
            "interleaved": {"field": "reasoning_content"},
            "limit": {"context": 131072, "output": 16384},
        }
        added += 1
    return result, added


class OpenCodeModelSync:
    def __init__(self, path: Path, base_url: str, backup_directory: Path | None = None) -> None:
        if path.suffix.lower() != ".json":
            raise OpenCodeSyncError("OpenCode sync requires a strict .json file, not JSONC")
        self.path = path.absolute()
        self.base_url = base_url
        self.backup_directory = backup_directory or cache_directory() / "opencode-backups"

    def __call__(self, model_ids: tuple[str, ...]) -> None:
        try:
            added = self.sync(model_ids)
            logger.info("OpenCode model sync complete: added=%s; existing entries preserved", added)
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
        updated, added = merge_models(config, model_ids, self.base_url)
        if not added:
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
            return added
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("OpenCode sync temporary-file cleanup failed")
