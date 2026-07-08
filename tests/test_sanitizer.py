from nvidia_nim_proxy.sanitizer import (
    NVIDIA_NIM_SAFE_CHAT_FIELDS,
    ProviderContext,
    is_nvidia_nim_provider,
    sanitize_chat_completion_body,
)


def test_detects_nvidia_nim_by_base_url() -> None:
    context = ProviderContext(base_url="https://integrate.api.nvidia.com/v1")

    assert is_nvidia_nim_provider(context) is True


def test_detects_nvidia_nim_by_provider_name() -> None:
    context = ProviderContext(provider_name="NVIDIA NIM")

    assert is_nvidia_nim_provider(context) is True


def test_nvidia_nim_strips_extra_body() -> None:
    body = {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "Say hello."}],
        "stream": True,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }

    sanitized = sanitize_chat_completion_body(
        body,
        ProviderContext(base_url="https://integrate.api.nvidia.com/v1"),
    )

    assert "extra_body" not in sanitized.body
    assert sanitized.stripped_keys == ("extra_body",)
    assert sanitized.body["stream"] is True


def test_nvidia_nim_keeps_only_safe_openai_compatible_fields() -> None:
    body = {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "Say hello."}],
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 128,
        "stream": False,
        "seed": 7,
        "stop": ["END"],
        "frequency_penalty": 0,
        "presence_penalty": 0,
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
        "parallel_tool_calls": False,
        "extraBody": {"chat_template_kwargs": {"enable_thinking": False}},
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
        "reasoning_effort": "low",
        "provider_options": {"zai": {"thinking": False}},
    }

    sanitized = sanitize_chat_completion_body(
        body,
        ProviderContext(provider_code="nim"),
    )

    assert set(sanitized.body) == NVIDIA_NIM_SAFE_CHAT_FIELDS
    assert sanitized.body["model"] == "z-ai/glm-5.2"
    assert sanitized.body["messages"] == [{"role": "user", "content": "Say hello."}]
    assert sanitized.body["stream"] is False
    assert sanitized.body["tool_choice"] == "auto"
    assert sanitized.body["parallel_tool_calls"] is False
    assert sanitized.body["tools"][0]["function"]["name"] == "read_file"
    assert sanitized.stripped_keys == (
        "chat_template_kwargs",
        "enable_thinking",
        "extraBody",
        "provider_options",
        "reasoning_effort",
    )


def test_non_nvidia_provider_is_unchanged() -> None:
    body = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "Say hello."}],
        "extra_body": {"provider_specific": True},
        "reasoning_effort": "medium",
    }

    sanitized = sanitize_chat_completion_body(body, ProviderContext(provider_name="openai"))

    assert sanitized.body == body
    assert sanitized.stripped_keys == ()


def test_nvidia_nim_streaming_mode_survives_sanitization() -> None:
    body = {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "Stream hello."}],
        "stream": True,
        "max_tokens": 128,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }

    sanitized = sanitize_chat_completion_body(body, ProviderContext(provider_name="nvidia"))

    assert sanitized.body == {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "Stream hello."}],
        "stream": True,
        "max_tokens": 128,
    }


def test_nvidia_glm_official_sample_fields_survive_sanitization() -> None:
    body = {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": ""}],
        "temperature": 1,
        "top_p": 1,
        "max_tokens": 16384,
        "seed": 42,
        "stream": True,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }

    sanitized = sanitize_chat_completion_body(
        body,
        ProviderContext(base_url="https://integrate.api.nvidia.com/v1"),
    )

    assert sanitized.body == {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": ""}],
        "temperature": 1,
        "top_p": 1,
        "max_tokens": 16384,
        "seed": 42,
        "stream": True,
    }
    assert sanitized.stripped_keys == ("extra_body",)


def test_nvidia_nim_keeps_openai_tool_calling_fields() -> None:
    body = {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "Use a tool."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "edit",
                    "description": "Edit text",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }

    sanitized = sanitize_chat_completion_body(body, ProviderContext(provider_code="nim"))

    assert "extra_body" not in sanitized.body
    assert sanitized.body["tools"][0]["function"]["name"] == "edit"
    assert sanitized.body["tool_choice"] == "auto"
    assert sanitized.body["parallel_tool_calls"] is True
