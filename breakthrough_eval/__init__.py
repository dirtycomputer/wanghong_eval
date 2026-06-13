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

from .eval.evaluator import Evaluator
from .prover.backends import (
    CodexProverBackend,
    DirectProverBackend,
    HermesProverBackend,
    OpenCodeProverBackend,
)

from .controller import Controller, JobMatrix  # noqa: E402
from .leaderboard import Leaderboard  # noqa: E402
from .logging_util import get_logger, setup_logging  # noqa: E402
from .specification import (  # noqa: E402
    EvalResult,
    Job,
    ModelRegistry,
    ModelEntry,
    ProverRunResult,
    TaskSpec,
    load_all_tasks,
    load_taskspec,
    validate_taskspec,
)
from .storage import ResultStore  # noqa: E402

__version__ = "0.1.0"

__all__ = [
    "TaskSpec",
    "Job",
    "ModelEntry",
    "ProverRunResult",
    "EvalResult",
    "ModelRegistry",
    "Controller",
    "JobMatrix",
    "Evaluator",
    "CodexProverBackend",
    "OpenCodeProverBackend",
    "HermesProverBackend",
    "DirectProverBackend",
    "Leaderboard",
    "ResultStore",
    "load_taskspec",
    "load_all_tasks",
    "validate_taskspec",
    "setup_logging",
    "get_logger",
]
