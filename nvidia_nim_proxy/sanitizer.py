"""Provider-specific request body sanitation for OpenAI-compatible APIs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from nvidia_nim_proxy.model_profiles import NVIDIA_REASONING_PROFILES, ReasoningProfile

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
    reasoning_policy: str | None = None


def _requested_effort(body: Mapping[str, Any], profile: ReasoningProfile) -> tuple[Any, str | None]:
    """Prefer explicit wire fields over legacy SDK wrappers; never merge payloads."""

    for prefix, source in (
        ("", body),
        ("extra_body.", body.get("extra_body")),
        ("extraBody.", body.get("extraBody")),
    ):
        if not isinstance(source, Mapping):
            continue
        if "reasoning_effort" in source:
            return source["reasoning_effort"], prefix + "reasoning_effort"
        template = source.get("chat_template_kwargs")
        if not isinstance(template, Mapping):
            continue
        if profile.location == "chat_template" and template.get("thinking") is False:
            return "none", prefix + "chat_template_kwargs.thinking"
        if "reasoning_effort" in template:
            return template["reasoning_effort"], prefix + "chat_template_kwargs.reasoning_effort"
    return None, None


def _apply_reasoning_profile(
    body: Mapping[str, Any],
    cleaned: dict[str, Any],
    stripped: set[str],
    profile: ReasoningProfile,
) -> str:
    if profile.location == "provider_default":
        return f"provider_default={profile.default} (no override sent)"

    requested, source = _requested_effort(body, profile)
    valid = isinstance(requested, str) and requested in profile.efforts
    effort = requested if valid else profile.default
    if valid and source == "reasoning_effort" and profile.location == "body":
        stripped.discard("reasoning_effort")
    elif source is not None and not valid:
        stripped.add(source)

    if profile.location == "body":
        if effort is not None:
            cleaned["reasoning_effort"] = effort
    else:
        # NVIDIA's DeepSeek snippets use SDK extra_body as a *merge argument*.
        # On the wire only these two chat-template fields may be forwarded.
        template: dict[str, Any] = {"thinking": effort != "none"}
        if effort != "none":
            template["reasoning_effort"] = effort
        cleaned["chat_template_kwargs"] = template
        original = body.get("chat_template_kwargs")
        if isinstance(original, Mapping):
            stripped.discard("chat_template_kwargs")
            stripped.update(
                f"chat_template_kwargs.{key}"
                for key in original
                if key not in {"thinking", "reasoning_effort"}
            )

    label = "client" if valid else "default"
    return f"{profile.location}={effort or 'provider_default'} ({label})"


def is_nvidia_nim_provider(context: ProviderContext) -> bool:
    """Return true when the provider identity points at NVIDIA NIM."""

    provider_identity = " ".join((context.provider_name.lower(), context.provider_code.lower()))
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
    stripped: set[str] = set()
    model = str(body.get("model", "")).lower()
    profile = NVIDIA_REASONING_PROFILES.get(model)
    excluded_fields = profile.excluded_fields if profile else frozenset()

    for key, value in body.items():
        if key in NVIDIA_NIM_SAFE_CHAT_FIELDS and key not in excluded_fields:
            cleaned[key] = value
        else:
            stripped.add(key)

    policy = _apply_reasoning_profile(body, cleaned, stripped, profile) if profile else None

    return SanitizedRequest(
        body=cleaned, stripped_keys=tuple(sorted(stripped)), reasoning_policy=policy
    )
