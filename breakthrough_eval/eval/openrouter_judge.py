"""Wire ``LLMJudge`` to a real OpenRouter model (plan §4.2).

EVAL is allowed to use the strongest closed/open API. This builds the
``complete(system, user) -> str`` callable that ``LLMJudge`` expects, backed by
an OpenRouter model (e.g. ``deepseek/deepseek-v4-pro``). It asks the model for a
JSON object so the verdict parses deterministically.
"""

from __future__ import annotations

from ..llm_client import OpenRouterClient
from .llm import LLMJudge


def make_openrouter_complete(model: str, **client_kwargs):
    client = OpenRouterClient(model=model, **client_kwargs)

    def complete(system: str, user: str) -> str:
        res = client.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        return res.content

    complete.client = client  # type: ignore[attr-defined]  # expose for availability checks
    return complete


def openrouter_judge(model: str, name: str | None = None, **client_kwargs) -> LLMJudge:
    return LLMJudge(
        complete=make_openrouter_complete(model, **client_kwargs),
        name=name or f"openrouter:{model}",
    )


def _factory(**kwargs) -> LLMJudge:
    if "model" not in kwargs:
        raise KeyError("openrouter judge 需要 model (e.g. deepseek/deepseek-v4-pro)")
    return openrouter_judge(kwargs["model"], name=kwargs.get("name"))
