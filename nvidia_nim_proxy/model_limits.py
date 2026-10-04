"""Reviewed NVIDIA model limits for OpenCode (2026-10-04).

These are documented model capacities, not a promise about every hosted
deployment. Keep output budgets modest: OpenCode reserves them before compacting.
Source links, hosted-test caveats and deliberate overrides are in README.md.
Author: Levent Dogan
"""

from types import MappingProxyType


MODEL_CONTEXT_LIMITS = MappingProxyType({
    "deepseek-ai/deepseek-v4.1-flash": 1048576,
    "google/gemma-4-31b-it": 262144,
    "meta/muse-glimmer-30b": 131072,
    "moonshotai/kimi-k2.6": 262144,
    "moonshotai/kimi-k3": 1048576,
    "nvidia/nemotron-3-super-120b-a12b": 1048576,
    "nvidia/nemotron-3-ultra-550b-a55b": 1048576,
    "nvidia/nemotron-3.5-lightning-30b-a3b": 1048576,
    "openai/gpt-oss-20b": 131072,
    "poolside/laguna-xs-2.1": 262144,
    "z-ai/glm-5.3": 1048576,
    "z-ai/glm-5.3-flash": 1048576,
})
LEGACY_MODEL_LIMIT = {"context": 131072, "output": 16384}


def model_limits(model_id: str) -> dict[str, int]:
    """Return a fresh limit object; unknown model IDs must not inherit 1M."""
    return {
        "context": MODEL_CONTEXT_LIMITS[model_id],
        # NVIDIA's hosted GPT-OSS schema caps max_tokens at 4096.
        # For other models 16384 is our operating budget, not their claimed maximum.
        "output": 4096 if model_id == "openai/gpt-oss-20b" else 16384,
    }
