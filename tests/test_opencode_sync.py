from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import nvidia_nim_proxy.opencode_sync as module
from nvidia_nim_proxy.opencode_sync import OpenCodeModelSync, OpenCodeSyncError, merge_models


URL = "http://127.0.0.1:8787/v1"
MODELS = ("moonshotai/kimi-k3", "z-ai/glm-5.3", "nvidia/embed-qa-4", "unknown/model")


def test_sync_creates_secret_free_gui_config_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "config" / "opencode.json"
    sync = OpenCodeModelSync(path, URL, tmp_path / "backups")
    assert sync.sync(MODELS) == 2
    original = path.read_bytes()
    config = json.loads(original)
    provider = config["provider"]["nim-local"]
    assert provider["options"] == {"baseURL": URL}
    assert set(provider["models"]) == {"moonshotai/kimi-k3", "z-ai/glm-5.3"}
    assert "apiKey" not in original.decode()
    assert "model" not in config
    assert sync.sync(MODELS) == 0
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".nim-sync-*"))


def test_preserves_manual_models_defaults_credentials_and_other_providers(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    config = {
        "model": "other/manual",
        "permission": {"edit": "ask"},
        "provider": {
            "other": {"options": {"apiKey": "private-other"}},
            "nim-local": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "my name",
                "options": {"baseURL": URL, "apiKey": "private-local", "timeout": 600000},
                "models": {
                    "moonshotai/kimi-k3": {"name": "custom", "options": {"reasoningEffort": "low"}},
                    "manual/model": {"name": "manually added"},
                },
            },
        },
    }
    original = json.dumps(config, indent=4).encode()
    path.write_bytes(original)
    backup_dir = tmp_path / "private-backups"
    sync = OpenCodeModelSync(path, URL, backup_dir)
    assert sync.sync(MODELS) == 1
    updated = json.loads(path.read_bytes())
    del updated["provider"]["nim-local"]["models"]["z-ai/glm-5.3"]
    assert updated == config
    backups = list(backup_dir.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    before = path.read_bytes()
    assert sync.sync(("z-ai/glm-5.3",)) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [b"// comment\n{}", b"{broken", b"[]", b"\xff", b'{"provider":{},"provider":{"secret":1}}'],
)
def test_invalid_or_jsonc_config_is_preserved(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "opencode.json"
    path.write_bytes(raw)
    with pytest.raises(OpenCodeSyncError):
        OpenCodeModelSync(path, URL).sync(MODELS)
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "provider",
    [
        None,
        [],
        {"npm": "@ai-sdk/openai", "options": {"baseURL": URL}},
        {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "https://other/v1"}},
        {"npm": "@ai-sdk/openai-compatible", "options": None},
        {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": URL}, "models": []},
    ],
)
def test_provider_collisions_fail_closed(provider: Any) -> None:
    with pytest.raises(OpenCodeSyncError):
        merge_models({"provider": {"nim-local": provider}}, MODELS, URL)


def test_no_models_no_file_created(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    sync = OpenCodeModelSync(path, URL)
    assert sync.sync(("nvidia/embed-qa-4", "other/unknown")) == 0
    assert not path.exists()


def test_sync_never_updates_key_files_or_jsonc(tmp_path: Path) -> None:
    for name in (".env", "opencode.jsonc"):
        with pytest.raises(OpenCodeSyncError):
            OpenCodeModelSync(tmp_path / name, URL)


def test_concurrent_gui_edit_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "opencode.json"
    path.write_text("{}")
    original_reader = module._read_config
    reads = 0
    gui_content = '{"model":"other/gui-selection"}'

    def reader(target: Path) -> bytes | None:
        nonlocal reads
        reads += 1
        if reads == 2:
            path.write_text(gui_content)
        return original_reader(target)

    monkeypatch.setattr(module, "_read_config", reader)
    sync = OpenCodeModelSync(path, URL, tmp_path / "backups")
    with pytest.raises(OpenCodeSyncError, match="changed during"):
        sync.sync(MODELS)
    assert path.read_text() == gui_content
    assert not list(tmp_path.glob(".nim-sync-*"))


def test_concurrent_creation_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "opencode.json"
    real_link = os.link

    def raced_link(source: Any, target: Any) -> None:
        path.write_text('{"model":"gui"}')
        real_link(source, target)

    monkeypatch.setattr(os, "link", raced_link)
    with pytest.raises(FileExistsError):
        OpenCodeModelSync(path, URL).sync(MODELS)
    assert json.loads(path.read_text()) == {"model": "gui"}


def test_callback_logs_no_private_settings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "opencode.json"
    path.write_text('{"apiKey":"private-secret", // private-message\n}')
    OpenCodeModelSync(path, URL)(MODELS)
    assert "private-" not in caplog.text
    assert "sync skipped" in caplog.text


def test_backup_failure_leaves_configuration_untouched(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    path.write_bytes(b"{}")
    backups = tmp_path / "not-a-directory"
    backups.write_text("blocked")
    with pytest.raises(OSError):
        OpenCodeModelSync(path, URL, backups).sync(MODELS)
    assert path.read_bytes() == b"{}"


def test_config_size_limit_and_bom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "opencode.json"
    path.write_text("{}", encoding="utf-8-sig")
    assert OpenCodeModelSync(path, URL, tmp_path / "backup").sync(MODELS) == 2
    monkeypatch.setattr(module, "MAX_CONFIG_BYTES", 10)
    with pytest.raises(OpenCodeSyncError, match="size limit"):
        OpenCodeModelSync(path, URL).sync(MODELS)
