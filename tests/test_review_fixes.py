"""Focused regression tests for prompts, semantic probes, and earned coverage."""

import json

from breakthrough_eval.eval.codex import build_eval_result
from breakthrough_eval.prover.contamination import SemanticProbeJudge
from breakthrough_eval.prover.prompts import probe_prompt, prove_prompt
from breakthrough_eval.specification import (
    AuditResult,
    JudgeVerdict,
    ProverRunResult,
    RubricItemVerdict,
)


def test_semantic_probe_judge_catches_paraphrase(task):
    judge = SemanticProbeJudge(
        complete=lambda s, u: json.dumps(
            {"leaked": True, "evidence": "换措辞复现了体积估计路线", "confidence": 0.9}
        )
    )
    leaked, notes = judge.assess(task, task.contamination_probes[0], "回答文本")
    assert leaked
    assert "换措辞" in notes


def test_semantic_probe_judge_abstains_on_garbage(task):
    judge = SemanticProbeJudge(complete=lambda s, u: "not json at all")
    leaked, notes = judge.assess(task, task.contamination_probes[0], "回答文本")
    assert not leaked
    assert "弃权" in notes


def test_semantic_judge_exception_does_not_break_probe(task):
    def boom(system, user):
        raise RuntimeError("network down")

    leaked, notes = SemanticProbeJudge(complete=boom).assess(
        task, task.contamination_probes[0], "回答文本"
    )
    assert not leaked
    assert "弃权" in notes


def test_golden_proof_text_never_reaches_prover(task):
    marker = "Wang–Zahl 证明结构概要"
    assert marker in task.golden_proof.proof_text
    for level in [h.level for h in task.hint_ladder]:
        assert marker not in prove_prompt(task, level)
    for p in task.contamination_probes:
        assert marker not in probe_prompt(task, p.id)


def _golden_verdict(task) -> JudgeVerdict:
    return JudgeVerdict(
        judge_name="codex-eval",
        item_verdicts=[
            RubricItemVerdict(item_id=item.id, passed=True, cited_lines=[1], confidence=0.9)
            for item in task.rubric
        ],
        overall_valid=True,
    )


def _prover(task, hint_level: int) -> ProverRunResult:
    return ProverRunResult(
        job_id=f"j{hint_level}",
        task_id=task.task_id,
        model="m",
        hint_level=hint_level,
        trial=0,
        harness="codex",
        raw_output="proof",
        audit=AuditResult(passed=True),
    )


def test_earned_coverage_excludes_revealed_items(task):
    res = build_eval_result(task, _prover(task, hint_level=3), _golden_verdict(task))
    assert res.passed_items == 7
    assert sorted(res.revealed_items) == ["R1", "R2", "R3", "R4"]
    assert res.earned_total_items == 3 and res.earned_passed_items == 3
    assert res.earned_coverage == 1.0


def test_earned_coverage_undefined_when_all_revealed(task):
    res = build_eval_result(task, _prover(task, hint_level=5), _golden_verdict(task))
    assert res.earned_total_items == 0
    assert res.earned_coverage is None


def test_cold_start_earned_equals_full_coverage(task):
    res = build_eval_result(task, _prover(task, hint_level=0), _golden_verdict(task))
    assert res.earned_total_items == res.total_items
    assert res.earned_coverage == res.rubric_coverage == 1.0
