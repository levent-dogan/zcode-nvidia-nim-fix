"""Verified NVIDIA hosted-API reasoning capabilities, not model-name guesses.

Author: Levent Dogan
Sources and the last verification date are recorded in README.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ReasoningProfile:
    location: Literal["body", "chat_template", "provider_default"]
    efforts: frozenset[str]
    default: str | None = None
    excluded_fields: frozenset[str] = frozenset()


_KIMI_K3 = ReasoningProfile(
    location="body",
    efforts=frozenset({"low", "high", "max"}),
    default="max",
    excluded_fields=frozenset({"top_p", "presence_penalty", "frequency_penalty"}),
)
_DEEPSEEK_V4 = ReasoningProfile(
    location="chat_template",
    efforts=frozenset({"none", "high", "max"}),
    default="max",
)
# The GLM model cards document max as the default, but the hosted request schemas
# do not expose an effort override. Do not infer wire support from the weights.
_GLM_53 = ReasoningProfile(location="provider_default", efforts=frozenset(), default="max")
_GPT_OSS = ReasoningProfile(location="body", efforts=frozenset({"low", "medium", "high"}))

NVIDIA_REASONING_PROFILES = {
    "moonshotai/kimi-k3": _KIMI_K3,
    "z-ai/glm-5.3": _GLM_53,
    "z-ai/glm-5.3-flash": _GLM_53,
    "deepseek-ai/deepseek-v4-pro": _DEEPSEEK_V4,
    "deepseek-ai/deepseek-v4-pro-0813": _DEEPSEEK_V4,
    "deepseek-ai/deepseek-v4-flash": _DEEPSEEK_V4,
    "deepseek-ai/deepseek-v4-flash-0731": _DEEPSEEK_V4,
    "openai/gpt-oss-20b": _GPT_OSS,
    "openai/gpt-oss-120b": _GPT_OSS,
}
