"""EVAL: judge, multi-judge consensus, κ, golden anchoring (plan §4)."""

import json

from breakthrough_eval.eval.evaluator import (
    Evaluator,
    cohen_kappa,
    golden_reference_text,
)
from breakthrough_eval.eval.llm import LLMJudge, build_judge_prompt
from breakthrough_eval.eval.mock import MockJudge, MockJudgeConfig


def _panel():
    return [
        MockJudge(MockJudgeConfig(strictness=0.4, name="lenient")),
        MockJudge(MockJudgeConfig(strictness=0.5, name="balanced")),
        MockJudge(MockJudgeConfig(strictness=0.7, name="strict")),
    ]


def test_cohen_kappa_extremes():
    assert cohen_kappa([True, True, False], [True, True, False]) == 1.0
    assert cohen_kappa([True, False, True], [False, True, False]) < 0.0


def test_golden_anchoring_full_marks(task):
    """Golden proof must score full coverage + overall valid (calibration)."""
    ev = Evaluator(_panel())
    result = ev.calibrate_with_golden(task)
    assert result.passed_items == result.total_items == len(task.rubric)
    assert result.overall_valid
    assert result.rubric_coverage == 1.0


def test_empty_proof_scores_zero(task):
    ev = Evaluator(_panel())
    res = ev.evaluate_text("j", task, "# Claimed Proof\n(nothing)")
    assert res.passed_items == 0
    assert not res.overall_valid


def test_partial_proof_triggers_disagreement(task):
    # 2/3 indicators per item: lenient/balanced pass (need 2), strict fails (need 3).
    lines = ["# Claimed Proof", "## 2. 关键引理"]
    for item in task.rubric:
        lines.append(f"- 引理 {item.id}: " + ", ".join(item.indicators[:2]))
    ev = Evaluator(_panel())
    res = ev.evaluate_text("j", task, "\n".join(lines))
    assert res.agreement < 1.0
    assert res.needs_human_review


def test_excluded_run_scored_excluded(task):
    from breakthrough_eval.models import ProverRunResult

    pr = ProverRunResult(
        job_id="j", task_id=task.task_id, model="m", hint_level=0, trial=0,
        harness="h", contaminated=True,
    )
    ev = Evaluator(_panel())
    res = ev.evaluate(pr, task)
    assert res.excluded
    assert not res.overall_valid


def test_errored_run_skips_judges_and_is_void(task):
    # 基础设施错误的 run 作废: 不送评委 (省 API), 标 errored 而非 excluded/0 分。
    from breakthrough_eval.models import ProverRunResult

    calls = {"n": 0}

    class CountingJudge(MockJudge):
        def judge(self, *a, **kw):
            calls["n"] += 1
            return super().judge(*a, **kw)

    pr = ProverRunResult(
        job_id="j", task_id=task.task_id, model="m", hint_level=0, trial=0,
        harness="h", error="LLMError: 网络错误",
    )
    ev = Evaluator([CountingJudge()])
    res = ev.evaluate(pr, task)
    assert res.errored
    assert not res.excluded
    assert not res.overall_valid and res.judges == []
    assert calls["n"] == 0


def test_llm_judge_parses_injected_client(task):
    payload = {
        "item_verdicts": [
            {"item_id": r.id, "passed": True, "cited_lines": [1], "confidence": 0.9}
            for r in task.rubric
        ],
        "overall_valid": True,
        "alternative_valid": False,
        "notes": "ok",
    }

    def fake_complete(system, user):
        assert "禁止笼统判断" in system  # §4.2 line-citation rule present
        assert "Rubric" in user
        return "```json\n" + json.dumps(payload) + "\n```"

    judge = LLMJudge(complete=fake_complete)
    verdict = judge.judge(task, "proof", task.golden_proof)
    assert verdict.overall_valid
    assert len(verdict.item_verdicts) == len(task.rubric)


def test_llm_judge_abstains_on_unparseable_output(task):
    # A judge that returns junk must NOT crash the run; it abstains + flags review.
    from breakthrough_eval.eval.llm import LLMJudge

    judge = LLMJudge(complete=lambda s, u: "totally not json", name="broken")
    verdict = judge.judge(task, "proof", task.golden_proof)
    assert verdict.parse_failed
    assert verdict.item_verdicts == []


def test_evaluator_survives_parse_failure(task):
    from breakthrough_eval.eval.llm import LLMJudge

    good = MockJudge(MockJudgeConfig(strictness=0.5))
    broken = LLMJudge(complete=lambda s, u: "<<garbage>>", name="broken")
    ev = Evaluator([good, broken])
    res = ev.evaluate_text("j", task, golden_reference_text(task))
    # broken judge abstains; good judge still scores; cell flagged for review
    assert res.needs_human_review
    assert res.passed_items == res.total_items  # good judge passes the golden ref
    assert any(v.parse_failed for v in res.judges)


def test_llm_judge_without_client_raises(task):
    judge = LLMJudge(complete=None)
    assert not judge.available()
    try:
        judge.judge(task, "x", task.golden_proof)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")
