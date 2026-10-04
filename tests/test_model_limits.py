from __future__ import annotations

import pytest

from nvidia_nim_proxy.model_limits import MODEL_CONTEXT_LIMITS, model_limits


@pytest.mark.parametrize("model,context,output", [
    ("deepseek-ai/deepseek-v4.1-flash", 1048576, 16384),
    ("google/gemma-4-31b-it", 262144, 16384),
    ("meta/muse-glimmer-30b", 131072, 16384),
    ("moonshotai/kimi-k2.6", 262144, 16384),
    ("moonshotai/kimi-k3", 1048576, 16384),
    ("nvidia/nemotron-3-super-120b-a12b", 1048576, 16384),
    ("nvidia/nemotron-3-ultra-550b-a55b", 1048576, 16384),
    ("nvidia/nemotron-3.5-lightning-30b-a3b", 1048576, 16384),
    ("openai/gpt-oss-20b", 131072, 4096),
    ("poolside/laguna-xs-2.1", 262144, 16384),
    ("z-ai/glm-5.3", 1048576, 16384),
    ("z-ai/glm-5.3-flash", 1048576, 16384),
])
def test_reviewed_model_limits(model: str, context: int, output: int) -> None:
    assert model_limits(model) == {"context": context, "output": output}
    assert 0 < output < context


def test_unknown_or_older_model_does_not_inherit_new_limits() -> None:
    for model in ("unknown/model", "z-ai/glm-5.2", "z-ai/glm-5.3[1m]"):
        with pytest.raises(KeyError):
            model_limits(model)


def test_limits_are_independent_copies() -> None:
    value = model_limits("moonshotai/kimi-k3")
    value["context"] = 1
    assert model_limits("moonshotai/kimi-k3")["context"] == 1048576
    assert MODEL_CONTEXT_LIMITS["moonshotai/kimi-k3"] == 1048576
