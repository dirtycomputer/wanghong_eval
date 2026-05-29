"""Judge backend interface + registry (plan §4)."""

from __future__ import annotations

from typing import Callable

from ..models import GoldenProof, JudgeVerdict, TaskSpec


class JudgeBackend:
    """Abstract LLM-as-judge backend.

    A judge sees ``proof_text`` + ``golden`` + ``task.rubric`` and returns a
    per-rubric-item verdict with line citations (plan §4.2). It is asked to judge
    "is this argument actually valid and does it reach the same theorem", with
    the golden proof only as an *alignment reference* (plan §4.1).
    """

    name: str = "abstract"

    def available(self) -> bool:
        return True

    def judge(
        self,
        task: TaskSpec,
        proof_text: str,
        golden: GoldenProof,
    ) -> JudgeVerdict:  # pragma: no cover - abstract
        raise NotImplementedError


_JUDGES: dict[str, Callable[..., JudgeBackend]] = {}


def register_judge(name: str, factory: Callable[..., JudgeBackend]) -> None:
    _JUDGES[name] = factory


def get_judge(name: str, **kwargs) -> JudgeBackend:
    if name not in _JUDGES:
        raise KeyError(f"未知 judge '{name}'. 已注册: {sorted(_JUDGES)}")
    return _JUDGES[name](**kwargs)
