"""Reviewed NVIDIA model limits for OpenCode (2026-11-05).

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
    # Hosted Build page advertises 262K despite the checkpoint's 1M model card.
    "minimaxai/minimax-m3.1-flash": 262144,
    # NVIDIA Build advertises 262K for this hosted Mistral deployment.
    "mistralai/mistral-large-4-1t-a52b": 262144,
    "moonshotai/kimi-k2.6": 262144,
    "moonshotai/kimi-k3": 1048576,
    "nvidia/nemotron-3-super-120b-a12b": 1048576,
    "nvidia/nemotron-3-ultra-550b-a55b": 1048576,
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": 262144,
    "nvidia/nemotron-3.5-bolt-e4b": 65536,
    "nvidia/nemotron-3.5-lightning-30b-a3b": 1048576,
    "nvidia/nemotron-3.5-super-vl-120b-a12b": 1000000,
    "openai/gpt-oss-20b": 131072,
    "poolside/laguna-xs-2.1": 262144,
    "stepfun-ai/step-5-preview": 1000000,
    "z-ai/glm-5.3": 1048576,
    "z-ai/glm-5.3-flash": 1048576,
})
# Keep historical capacities available for custom deployments, but never advertise
# these reviewed deprecated free endpoints in the hosted OpenCode providers.
RETIRED_HOSTED_MODELS = frozenset({
    "moonshotai/kimi-k3",
    "nvidia/nemotron-3-super-120b-a12b",
    "z-ai/glm-5.3",
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
