"""Provider-specific request body sanitation for OpenAI-compatible APIs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


NVIDIA_NIM_SAFE_CHAT_FIELDS = frozenset(
    {
        "model",
        "messages",
        "temperature",
        "top_p",
        "max_tokens",
        "stream",
        "stream_options",
        "seed",
        "stop",
        "frequency_penalty",
        "presence_penalty",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
    }
)
NVIDIA_NIM_REASONING_MODEL_MARKERS = ("gpt-oss",)


@dataclass(frozen=True)
class ProviderContext:
    """Small provider descriptor used by sanitizers."""

    provider_name: str = ""
    provider_code: str = ""
    base_url: str = ""


@dataclass(frozen=True)
class SanitizedRequest:
    """Sanitizer output and debug-safe metadata."""

    body: dict[str, Any]
    stripped_keys: tuple[str, ...]


def is_nvidia_nim_provider(context: ProviderContext) -> bool:
    """Return true when the provider identity points at NVIDIA NIM."""

    provider_identity = " ".join(
        (context.provider_name.lower(), context.provider_code.lower())
    )
    base_url = context.base_url.lower()
    return (
        "integrate.api.nvidia.com" in base_url
        or "nvidia" in provider_identity
        or re.search(r"(^|[^a-z0-9])nim([^a-z0-9]|$)", provider_identity) is not None
    )


def sanitize_chat_completion_body(
    body: Mapping[str, Any],
    context: ProviderContext,
) -> SanitizedRequest:
    """Sanitize a chat completion JSON body for a specific provider.

    NVIDIA NIM rejects provider-extension fields such as ``extra_body`` at the
    top level. For NIM we keep a conservative OpenAI-compatible chat completion
    field set. Other providers are intentionally left unchanged.
    """

    if not is_nvidia_nim_provider(context):
        return SanitizedRequest(body=dict(body), stripped_keys=())

    cleaned: dict[str, Any] = {}
    stripped: list[str] = []
    model = str(body.get("model", "")).lower()
    supports_reasoning_effort = any(
        marker in model for marker in NVIDIA_NIM_REASONING_MODEL_MARKERS
    )

    for key, value in body.items():
        if key in NVIDIA_NIM_SAFE_CHAT_FIELDS or (
            key == "reasoning_effort" and supports_reasoning_effort
        ):
            cleaned[key] = value
        else:
            stripped.append(key)

    return SanitizedRequest(body=cleaned, stripped_keys=tuple(sorted(stripped)))
