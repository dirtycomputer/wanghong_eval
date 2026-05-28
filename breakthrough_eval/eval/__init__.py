"""EVAL subsystem (plan §4): LLM-as-judge + rubric win rate.

EVAL is the mirror of PROVER: it *is* allowed to know the answer (golden proof),
may be online and use the strongest closed API. Backends are pluggable;
``MockJudge`` is the default offline judge.
"""

from .base import JudgeBackend, get_judge, register_judge
from .evaluator import Evaluator
from .llm import LLMJudge
from .mock import MockJudge

__all__ = [
    "JudgeBackend",
    "get_judge",
    "register_judge",
    "Evaluator",
    "LLMJudge",
    "MockJudge",
]
