"""通用「突破复现」评测系统 (breakthrough-reproduction evaluation system).

把 Hassabis 的 Einstein Test 工程化为「时间冻结的开卷研究考试」:
Controller 量产 (突破问题, golden proof, rubric, hint 阶梯), PROVER 在断网 +
冻结 arXiv 下先过探针再应考, EVAL 用强评委 + rubric 给出 win rate, leaderboard
的真正信息量在「需要多少 hint 才解得出」这条难度曲线上。

整套系统的成败全押在污染防控这一根弦上 (plan §0)。
"""

from __future__ import annotations

import logging as _logging

# Library is quiet by default; the CLI / caller opts in via setup_logging.
_logging.getLogger("breakthrough_eval").addHandler(_logging.NullHandler())

# Register pluggable backends on import.
from .eval.base import register_judge
from .eval.evaluator import Evaluator
from .eval.llm import LLMJudge
from .eval.mock import MockJudge, _factory as _mock_judge_factory
from .eval.openrouter_judge import _factory as _openrouter_judge_factory, openrouter_judge
from .prover.base import register_backend
from .prover.codex import CodexProverBackend, _factory as _codex_factory
from .prover.mock import MockProverBackend, _factory as _mock_prover_factory
from .prover.openrouter import OpenRouterProverBackend, _factory as _openrouter_prover_factory

register_backend("mock", _mock_prover_factory)
register_backend("codex", _codex_factory)
register_backend("local-vllm", _codex_factory)  # codex backend points at local vLLM
register_backend("openrouter", _openrouter_prover_factory)  # real model via OpenRouter
register_judge("mock", _mock_judge_factory)
register_judge("openrouter", _openrouter_judge_factory)  # real strong judge via OpenRouter

from .arxiv_frozen import ArxivFrozenSource, InMemoryArxivSource  # noqa: E402
from .controller import BackendSpec, Controller, JobMatrix  # noqa: E402
from .leaderboard import Leaderboard  # noqa: E402
from .logging_util import get_logger, setup_logging  # noqa: E402
from .models import (  # noqa: E402
    EvalResult,
    Job,
    ModelEntry,
    ProverRunResult,
    TaskSpec,
)
from .registry import ModelRegistry  # noqa: E402
from .storage import ResultStore  # noqa: E402
from .taskspec import load_all_tasks, load_taskspec, validate_taskspec  # noqa: E402

__version__ = "0.1.0"

__all__ = [
    "TaskSpec",
    "Job",
    "ModelEntry",
    "ProverRunResult",
    "EvalResult",
    "ModelRegistry",
    "Controller",
    "BackendSpec",
    "JobMatrix",
    "Evaluator",
    "MockJudge",
    "LLMJudge",
    "openrouter_judge",
    "MockProverBackend",
    "CodexProverBackend",
    "OpenRouterProverBackend",
    "Leaderboard",
    "ResultStore",
    "ArxivFrozenSource",
    "InMemoryArxivSource",
    "load_taskspec",
    "load_all_tasks",
    "validate_taskspec",
    "setup_logging",
    "get_logger",
]
