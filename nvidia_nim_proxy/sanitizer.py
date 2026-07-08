"""Provider-specific request body sanitation for OpenAI-compatible APIs."""

from __future__ import annotations

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
        "seed",
        "stop",
        "frequency_penalty",
        "presence_penalty",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
    }
)


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

    identity = " ".join(
        (
            context.provider_name.lower(),
            context.provider_code.lower(),
            context.base_url.lower(),
        )
    )
    return (
        "integrate.api.nvidia.com" in identity
        or "nvidia" in identity
        or "nim" in identity
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

    for key, value in body.items():
        if key in NVIDIA_NIM_SAFE_CHAT_FIELDS:
            cleaned[key] = value
        else:
            stripped.append(key)

    return SanitizedRequest(body=cleaned, stripped_keys=tuple(sorted(stripped)))
