"""Codex-backed EVAL artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from breakthrough_eval.eval.codex import CodexEvalAgent, CodexEvalConfig, build_eval_result
from breakthrough_eval.eval.evaluator import Evaluator
from breakthrough_eval.specification import (
    ApiConfig,
    AuditResult,
    JudgeVerdict,
    ProverRunResult,
    RubricItemVerdict,
)


def _prover(task, **overrides) -> ProverRunResult:
    data = dict(
        job_id="task::model::L0::t0",
        task_id=task.task_id,
        model="model",
        hint_level=0,
        trial=0,
        harness="codex",
        raw_output="# Claimed Proof\nThis is the prover answer.",
        audit=AuditResult(passed=True),
    )
    data.update(overrides)
    return ProverRunResult(**data)


def _config(binary: Path, home_dir: Path) -> CodexEvalConfig:
    return CodexEvalConfig(
        api=ApiConfig(base_url="http://127.0.0.1:8083", api_key="sk-test", model="gpt-5.5"),
        home_dir=home_dir,
        timeout_seconds=30,
        binary=str(binary),
    )


def _fake_codex(path: Path, payload: dict) -> None:
    script = f"""#!/usr/bin/env python3
import json
from pathlib import Path

Path("output").mkdir(exist_ok=True)
Path("output/result.json").write_text({json.dumps(json.dumps(payload))}, encoding="utf-8")
print(json.dumps({{"event": "done"}}))
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def test_codex_eval_writes_input_and_reads_result(tmp_path, task):
    payload = {
        "item_verdicts": [
            {
                "item_id": item.id,
                "passed": True,
                "justification": "cited",
                "cited_lines": [1],
                "confidence": 0.9,
            }
            for item in task.rubric
        ],
        "overall_valid": True,
        "alternative_valid": False,
        "needs_human_review": False,
        "notes": "ok",
    }
    binary = tmp_path / "codex"
    _fake_codex(binary, payload)

    evaluator = Evaluator(CodexEvalAgent(_config(binary, tmp_path / "codex_home")), tmp_path / "eval")
    result = evaluator.evaluate(_prover(task), task)

    job_dir = tmp_path / "eval"
    assert (job_dir / "input" / "task.md").exists()
    assert (job_dir / "input" / "prover.md").read_text(encoding="utf-8").startswith("1: # Claimed Proof")
    assert (job_dir / "output" / "result.json").exists()
    assert result.overall_valid
    assert result.passed_items == result.total_items == len(task.rubric)
    assert result.judges[0].judge_name == "codex-eval"


def test_errored_prover_skips_codex(tmp_path, task):
    binary = tmp_path / "missing-codex"
    evaluator = Evaluator(CodexEvalAgent(_config(binary, tmp_path / "codex_home")), tmp_path / "eval")
    result = evaluator.evaluate(_prover(task, error="backend failed"), task)
    assert result.errored
    assert result.judges == []
    assert not (tmp_path / "eval").exists()


def test_build_eval_result_counts_earned_coverage(task):
    prover = _prover(task, hint_level=3)
    judge = JudgeVerdict(
        judge_name="codex-eval",
        item_verdicts=[
            RubricItemVerdict(item_id=item.id, passed=True, cited_lines=[1], confidence=0.9)
            for item in task.rubric
        ],
        overall_valid=True,
    )
    result = build_eval_result(task, prover, judge)
    assert sorted(result.revealed_items) == ["R1", "R2", "R3", "R4"]
    assert result.earned_total_items == 3
    assert result.earned_passed_items == 3
    assert result.earned_coverage == 1.0
