"""Evaluator backed by a Codex agent."""

from __future__ import annotations

import logging
from pathlib import Path

from ..specification import EvalResult, ProverRunResult, TaskSpec
from .codex import DEFAULT_EVAL_CONFIG, CodexEvalAgent, CodexEvalConfig, errored_eval_result

log = logging.getLogger(__name__)


class Evaluator:
    def __init__(self, agent: CodexEvalAgent, eval_root: str | Path):
        self.agent = agent
        self.eval_root = Path(eval_root)

    @classmethod
    def load(cls, eval_root: str | Path, config_path: str | Path | None = None) -> "Evaluator":
        return cls(CodexEvalAgent(CodexEvalConfig.load(config_path or DEFAULT_EVAL_CONFIG)), eval_root)

    def describe(self) -> dict:
        return self.agent.describe()

    def evaluate(self, result: ProverRunResult, task: TaskSpec) -> EvalResult:
        if result.excluded:
            return EvalResult(
                job_id=result.job_id,
                task_id=task.task_id,
                total_items=len(task.rubric),
                excluded=True,
                needs_human_review=False,
            )
        if result.error is not None:
            return EvalResult(
                job_id=result.job_id,
                task_id=task.task_id,
                total_items=len(task.rubric),
                errored=True,
                needs_human_review=False,
            )
        job_dir = self.eval_root
        try:
            eval_result = self.agent.run(task, result, job_dir)
            log.info("eval %s: valid=%s coverage=%.2f", result.job_id, eval_result.overall_valid, eval_result.rubric_coverage)
            return eval_result
        except Exception as exc:  # noqa: BLE001 - eval infra error is a void run.
            error = f"{type(exc).__name__}: {exc}"
            log.error("eval %s failed: %s", result.job_id, error)
            return errored_eval_result(task, result, error)
