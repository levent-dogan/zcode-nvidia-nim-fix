"""Exercise Windows PowerShell 5.1 without starting a proxy or loading user keys."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")
ROOT = Path(__file__).resolve().parents[1]


def launch(tmp_path: Path, parameters: str) -> subprocess.CompletedProcess[str]:
    shutil.copyfile(ROOT / "run_proxy.ps1", tmp_path / "run_proxy.ps1")
    activation = tmp_path / ".venv" / "Scripts" / "Activate.ps1"
    activation.parent.mkdir(parents=True, exist_ok=True)
    activation.write_text("# Isolated test activation.\n", encoding="utf-8")
    script = r"""
    $env:NVIDIA_API_KEY = $null
    function global:python {
        Write-Output ('LAUNCH_ARGS:' + (ConvertTo-Json -InputObject @($args) -Compress))
        $global:LASTEXITCODE = 0
    }
    & .\run_proxy.ps1 PARAMETERS
    """.replace("PARAMETERS", parameters)
    return subprocess.run(
        [
            POWERSHELL or "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def forwarded(
    result: subprocess.CompletedProcess[str], expected_exit_code: int = 0
) -> list[str | int]:
    assert result.returncode == expected_exit_code, result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("LAUNCH_ARGS:"))
    values: list[str | int] = json.loads(line.removeprefix("LAUNCH_ARGS:"))
    assert values[:2] == ["-m", "nvidia_nim_proxy.server"]
    return values


def test_default_launcher_client_mode(tmp_path: Path) -> None:
    args = forwarded(launch(tmp_path, "-ApiKeyMode Client"))
    assert args[args.index("--api-key-mode") + 1] == "client"
    assert args[args.index("--model-refresh-seconds") + 1] == 21600
    assert "--debug" not in args
    assert "--opencode-config" not in args
    assert "--no-model-discovery" not in args


def test_launcher_forwards_debug_and_sync_path_with_spaces(tmp_path: Path) -> None:
    args = forwarded(
        launch(
            tmp_path,
            "-ApiKeyMode Client -DebugMode -ModelRefreshSeconds 3600 "
            "-OpenCodeConfig 'gui settings\\opencode.json'",
        )
    )
    assert "--debug" in args
    assert args[args.index("--model-refresh-seconds") + 1] == 3600
    assert args[args.index("--opencode-config") + 1] == str(
        tmp_path / "gui settings" / "opencode.json"
    )


def test_launcher_disables_discovery(tmp_path: Path) -> None:
    args = forwarded(launch(tmp_path, "-ApiKeyMode Client -DisableModelDiscovery"))
    assert "--no-model-discovery" in args


@pytest.mark.parametrize(
    "parameters",
    [
        "-ApiKeyMode Client -DisableModelDiscovery -OpenCodeConfig opencode.json",
        "-ApiKeyMode Client -ModelRefreshSeconds 0",
        "-ApiKeyMode Env",
    ],
)
def test_launcher_rejects_invalid_startup(tmp_path: Path, parameters: str) -> None:
    result = launch(tmp_path, parameters)
    assert result.returncode != 0
    assert "LAUNCH_ARGS:" not in result.stdout


def test_pool_launcher_does_not_forward_or_print_keys(tmp_path: Path) -> None:
    local_key = "test-only-local-credential"
    upstream_key = "test-only-upstream-credential"
    (tmp_path / ".env").write_text(
        f"NIM_PROXY_CLIENT_KEY={local_key}\nNVIDIA_API_KEY_1={upstream_key}\n",
        encoding="utf-8",
    )
    result = launch(tmp_path, "-ApiKeyMode Pool -DebugMode -OpenCodeConfig opencode.json")
    args = forwarded(result)
    assert args[args.index("--api-key-mode") + 1] == "pool"
    assert "--debug" in args
    assert local_key not in result.stdout + result.stderr
    assert upstream_key not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["pool", "client"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_debug_batch_launchers_sync_from_other_directory(
    tmp_path: Path, mode: str, exit_code: int
) -> None:
    repo = tmp_path / "proxy launch ! (test)"
    repo.mkdir()
    outside = tmp_path / "outside cwd"
    outside.mkdir()
    profile = tmp_path / "user ! profile (test)"
    profile.mkdir()
    name = f"start_proxy_{mode}_debug.bat"
    for filename in (name, "run_proxy.ps1"):
        shutil.copyfile(ROOT / filename, repo / filename)
    activation = repo / ".venv" / "Scripts" / "Activate.ps1"
    activation.parent.mkdir(parents=True)
    activation.write_text(
        """
function global:python {
    Write-Output ('LAUNCH_ARGS:' + (ConvertTo-Json -InputObject @($args) -Compress))
    Write-Output ('LAUNCH_CWD:' + (Get-Location).Path)
    $global:LASTEXITCODE = [int]$env:PROXY_TEST_EXIT_CODE
}
""",
        encoding="utf-8",
    )
    (repo / ".env").write_text(
        "NIM_PROXY_CLIENT_KEY=test-local-secret\nNVIDIA_API_KEY_1=test-upstream-secret\n",
        encoding="utf-8",
    )
    env = {**os.environ, "USERPROFILE": str(profile), "PROXY_TEST_EXIT_CODE": str(exit_code)}
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/v:off", "/c", str(repo / name)],
        cwd=outside,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    args = forwarded(result, exit_code)
    assert args[args.index("--api-key-mode") + 1] == mode
    assert "--debug" in args
    assert args[args.index("--upstream-timeout-seconds") + 1] == 600
    assert args[args.index("--opencode-config") + 1] == str(
        profile / ".config" / "opencode" / "opencode.json"
    )
    assert "LAUNCH_CWD:" + str(repo) in result.stdout
    assert "test-local-secret" not in result.stdout + result.stderr
    assert "test-upstream-secret" not in result.stdout + result.stderr
    assert not (profile / ".config").exists()
