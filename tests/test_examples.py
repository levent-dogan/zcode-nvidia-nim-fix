import json
from pathlib import Path

from nvidia_nim_proxy.model_profiles import NVIDIA_REASONING_PROFILES


def test_opencode_example_uses_supported_models_without_credentials() -> None:
    path = Path(__file__).resolve().parents[1] / "examples" / "opencode.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    provider = config["provider"]["nim-local"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {"baseURL": "http://127.0.0.1:8787/v1"}
    assert config["model"].removeprefix("nim-local/") in provider["models"]
    for model, settings in provider["models"].items():
        assert model in NVIDIA_REASONING_PROFILES
        assert settings["reasoning"] is True
        assert settings["tool_call"] is True
        assert settings["interleaved"] == {"field": "reasoning_content"}
        assert settings["limit"] == {"context": 131072, "output": 16384}
