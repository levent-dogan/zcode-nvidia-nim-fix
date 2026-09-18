from copy import deepcopy
from typing import Any

import pytest

from nvidia_nim_proxy.model_profiles import NVIDIA_REASONING_PROFILES
from nvidia_nim_proxy.sanitizer import ProviderContext, sanitize_chat_completion_body


NIM = ProviderContext(provider_name="NVIDIA NIM")
DEEPSEEK_MODELS = [
    "deepseek-ai/deepseek-v4-pro",
    "deepseek-ai/deepseek-v4-pro-0813",
    "deepseek-ai/deepseek-v4-flash",
    "deepseek-ai/deepseek-v4-flash-0731",
]


def test_kimi_defaults_to_max_and_removes_fixed_sampling_fields() -> None:
    result = sanitize_chat_completion_body(
        {
            "model": "moonshotai/kimi-k3",
            "messages": [],
            "stream": True,
            "max_tokens": 1024,
            "temperature": 1,
            "top_p": 0.9,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        },
        NIM,
    )
    assert result.body == {
        "model": "moonshotai/kimi-k3",
        "messages": [],
        "stream": True,
        "max_tokens": 1024,
        "temperature": 1,
        "reasoning_effort": "max",
    }
    assert result.stripped_keys == ("frequency_penalty", "presence_penalty", "top_p")
    assert result.reasoning_policy == "body=max (default)"


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_kimi_preserves_explicit_supported_effort(effort: str) -> None:
    result = sanitize_chat_completion_body(
        {"model": "moonshotai/kimi-k3", "reasoning_effort": effort}, NIM
    )
    assert result.body["reasoning_effort"] == effort
    assert result.stripped_keys == ()
    assert result.reasoning_policy == f"body={effort} (client)"


@pytest.mark.parametrize("model", DEEPSEEK_MODELS)
def test_deepseek_defaults_to_max_in_template_not_literal_extra_body(model: str) -> None:
    result = sanitize_chat_completion_body({"model": model, "messages": [], "stream": True}, NIM)
    assert result.body == {
        "model": model,
        "messages": [],
        "stream": True,
        "chat_template_kwargs": {"thinking": True, "reasoning_effort": "max"},
    }
    assert result.reasoning_policy == "chat_template=max (default)"


@pytest.mark.parametrize("effort", ["none", "high", "max"])
@pytest.mark.parametrize("wrapper", [None, "extra_body", "extraBody"])
@pytest.mark.parametrize("nested", [False, True])
def test_supported_options_are_normalized(effort: str, wrapper: str | None, nested: bool) -> None:
    options: dict[str, Any] = {"reasoning_effort": effort}
    if nested:
        options = {"chat_template_kwargs": options}
    if wrapper:
        options = {wrapper: options}
    body = {"model": DEEPSEEK_MODELS[0], **options}
    before = deepcopy(body)
    result = sanitize_chat_completion_body(body, NIM)
    expected: dict[str, Any] = {"thinking": effort != "none"}
    if effort != "none":
        expected["reasoning_effort"] = effort
    assert result.body == {"model": DEEPSEEK_MODELS[0], "chat_template_kwargs": expected}
    assert body == before


@pytest.mark.parametrize("wrapper", ["extra_body", "extraBody"])
def test_wrappers_cannot_override_model_messages_or_unrelated_settings(wrapper: str) -> None:
    result = sanitize_chat_completion_body(
        {
            "model": DEEPSEEK_MODELS[0],
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 32,
            wrapper: {
                "model": "another-model",
                "messages": [],
                "max_tokens": 1000000,
                "api_key": "private",
                "chat_template_kwargs": {
                    "thinking": True,
                    "reasoning_effort": "high",
                    "unknown": "private",
                },
            },
        },
        NIM,
    )
    assert result.body == {
        "model": DEEPSEEK_MODELS[0],
        "messages": [{"role": "user", "content": "test"}],
        "max_tokens": 32,
        "chat_template_kwargs": {"thinking": True, "reasoning_effort": "high"},
    }
    assert "private" not in str(result)
    assert wrapper in result.stripped_keys


def test_top_level_effort_takes_precedence_over_nested_and_wrapped_options() -> None:
    result = sanitize_chat_completion_body(
        {
            "model": DEEPSEEK_MODELS[0],
            "reasoning_effort": "high",
            "chat_template_kwargs": {"thinking": False, "reasoning_effort": "max"},
            "extra_body": {"reasoning_effort": "none"},
        },
        NIM,
    )
    assert result.body["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "high"}


def test_explicit_thinking_false_disables_deepseek_reasoning() -> None:
    result = sanitize_chat_completion_body(
        {
            "model": DEEPSEEK_MODELS[0],
            "chat_template_kwargs": {"thinking": False, "reasoning_effort": "max"},
        },
        NIM,
    )
    assert result.body["chat_template_kwargs"] == {"thinking": False}


@pytest.mark.parametrize("value", [None, "default", "medium", "secret-value", [], {}, True, 42])
@pytest.mark.parametrize("model", ["moonshotai/kimi-k3", DEEPSEEK_MODELS[0]])
def test_invalid_effort_uses_max_without_leaking_arbitrary_values(value: Any, model: str) -> None:
    result = sanitize_chat_completion_body({"model": model, "reasoning_effort": value}, NIM)
    options = result.body.get("chat_template_kwargs", result.body)
    assert options["reasoning_effort"] == "max"
    assert "reasoning_effort" in result.stripped_keys
    assert "secret-value" not in str(result)


@pytest.mark.parametrize("wrapper", [None, "extra_body", "extraBody"])
@pytest.mark.parametrize("malformed", [None, [], "private", True, 42])
def test_malformed_extensions_are_not_forwarded(wrapper: str | None, malformed: Any) -> None:
    options: dict[str, Any] = {"chat_template_kwargs": malformed}
    if wrapper:
        options = {wrapper: options}
    result = sanitize_chat_completion_body({"model": DEEPSEEK_MODELS[0], **options}, NIM)
    assert result.body["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "max"}
    assert "private" not in str(result)


def test_only_documented_deepseek_template_fields_survive() -> None:
    result = sanitize_chat_completion_body(
        {
            "model": DEEPSEEK_MODELS[0],
            "chat_template_kwargs": {
                "reasoning_effort": "max",
                "enable_thinking": True,
                "unknown": "private",
            },
        },
        NIM,
    )
    assert result.body["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "max"}
    assert result.stripped_keys == (
        "chat_template_kwargs.enable_thinking",
        "chat_template_kwargs.unknown",
    )


@pytest.mark.parametrize("model", ["z-ai/glm-5.3", "z-ai/glm-5.3-flash"])
def test_glm_uses_documented_native_max_without_unverified_wire_override(model: str) -> None:
    result = sanitize_chat_completion_body(
        {
            "model": model,
            "messages": [],
            "max_tokens": 24000,
            "reasoning_effort": "max",
            "extra_body": {"reasoning_effort": "max"},
            "chat_template_kwargs": {"reasoning_effort": "max"},
        },
        NIM,
    )
    assert result.body == {"model": model, "messages": [], "max_tokens": 24000}
    assert result.reasoning_policy == "provider_default=max (no override sent)"


@pytest.mark.parametrize(
    "model",
    [
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.6",
        "deepseek-ai/deepseek-r1",
        "deepseek-ai/deepseek-r1-0528",
        "deepseek-ai/deepseek-v3.1",
        "deepseek-ai/deepseek-v3.2",
        "deepseek-ai/deepseek-coder-6.7b-instruct",
        "deepseek-ai/deepseek-v4-future",
        "untrusted/gpt-oss-copy",
        "unknown/model",
    ],
)
def test_unknown_and_older_models_keep_provider_defaults(model: str) -> None:
    result = sanitize_chat_completion_body(
        {
            "model": model,
            "messages": [],
            "reasoning_effort": "max",
            "extra_body": {"chat_template_kwargs": {"thinking": True, "reasoning_effort": "max"}},
        },
        NIM,
    )
    assert result.body == {"model": model, "messages": []}
    assert result.reasoning_policy is None


@pytest.mark.parametrize("model", ["openai/gpt-oss-20b", "openai/gpt-oss-120b"])
def test_gpt_oss_does_not_receive_unsupported_max(model: str) -> None:
    for options in ({}, {"reasoning_effort": "max"}):
        result = sanitize_chat_completion_body({"model": model, **options}, NIM)
        assert result.body == {"model": model}
    result = sanitize_chat_completion_body({"model": model, "reasoning_effort": "medium"}, NIM)
    assert result.body["reasoning_effort"] == "medium"


@pytest.mark.parametrize("model", list(NVIDIA_REASONING_PROFILES))
def test_profiles_do_not_modify_messages_tools_or_resanitize_differently(model: str) -> None:
    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "test"}]},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "preserved",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "tool-result"},
        ],
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    before = deepcopy(body)
    result = sanitize_chat_completion_body(body, NIM)
    assert body == before
    assert result.body["messages"] == before["messages"]
    assert result.body["tools"] == before["tools"]
    assert result.body["stream_options"] == before["stream_options"]
    assert sanitize_chat_completion_body(result.body, NIM).body == result.body


@pytest.mark.parametrize("provider", ["openai", "anthropic", "z.ai", "openrouter"])
def test_same_model_ids_on_other_providers_are_untouched(provider: str) -> None:
    body = {
        "model": "moonshotai/kimi-k3",
        "reasoning_effort": "other",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    result = sanitize_chat_completion_body(body, ProviderContext(provider_name=provider))
    assert result.body == body
    assert result.reasoning_policy is None
    assert result.stripped_keys == ()
