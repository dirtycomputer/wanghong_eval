"""TaskSpec loading + the make-or-break red-line invariant (plan §2, §8)."""

import pytest
from pydantic import ValidationError

from breakthrough_eval.models import (
    GoldenProof,
    HintLevel,
    RetrievalConfig,
    RubricItem,
    TaskSpec,
)
from breakthrough_eval.taskspec import validate_taskspec


def _base_kwargs(**over):
    kw = dict(
        task_id="t",
        title="t",
        domain="math",
        breakthrough_date="2025-02-25",
        retrieval_cutoff="2025-01-31",
        allowed_model_cutoff_before="2025-01-31",
        problem_statement="prove it",
        golden_proof=GoldenProof(primary="arXiv:x"),
        rubric=[RubricItem(id="R1", title="r1", criterion="c", indicators=["a"])],
        hint_ladder=[HintLevel(level=0, label="L0", ratio=0.0)],
        retrieval=RetrievalConfig(),
    )
    kw.update(over)
    return kw


def test_real_task_loads(task):
    assert task.task_id == "kakeya_3d_wang_zahl"
    assert task.max_hint_level == 5
    assert any(r.frontier_delta for r in task.rubric)


def test_real_task_validates_clean():
    assert validate_taskspec("tasks/kakeya_3d_wang_zahl.yaml") == []


def test_redline_rejects_cutoff_after_breakthrough():
    # retrieval_cutoff must be < breakthrough_date
    with pytest.raises(ValidationError):
        TaskSpec(**_base_kwargs(retrieval_cutoff="2025-03-01"))


def test_redline_rejects_model_cutoff_after_retrieval():
    with pytest.raises(ValidationError):
        TaskSpec(**_base_kwargs(allowed_model_cutoff_before="2025-02-15"))


def test_hint_ladder_must_start_at_zero():
    with pytest.raises(ValidationError):
        TaskSpec(**_base_kwargs(hint_ladder=[HintLevel(level=1, label="L1", ratio=0.1)]))


def test_web_search_must_be_disabled():
    with pytest.raises(ValidationError):
        TaskSpec(**_base_kwargs(retrieval=RetrievalConfig(web_search="ENABLED")))


def test_leak_indicator_in_framing_notes_flagged(tmp_path):
    # 泄露词扫描覆盖 problem_framing_notes; 多个探针共享同一禁词只报一次。
    import yaml

    spec = dict(
        task_id="t",
        title="t",
        domain="math",
        breakthrough_date="2025-02-25",
        retrieval_cutoff="2025-01-31",
        allowed_model_cutoff_before="2025-01-31",
        problem_statement="prove it",
        problem_framing_notes="注意 secret-structure-theorem 这个词。",
        golden_proof={"primary": "arXiv:x"},
        rubric=[{"id": "R1", "title": "r1", "criterion": "c", "indicators": ["a"]}],
        contamination_probes=[
            {"id": "P1", "kind": "direct", "prompt": "q",
             "leak_indicators": ["secret-structure-theorem"]},
            {"id": "P2", "kind": "indirect", "prompt": "q2",
             "leak_indicators": ["secret-structure-theorem"]},
        ],
        hint_ladder=[{"level": 0, "label": "L0", "ratio": 0.0}],
    )
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    leaks = [i for i in validate_taskspec(p) if "泄露探针关键词" in i]
    assert len(leaks) == 1
    assert "secret-structure-theorem" in leaks[0]


def test_hint_referencing_unknown_rubric_item_rejected():
    bad_hints = [
        HintLevel(level=0, label="L0", ratio=0.0),
        HintLevel(level=1, label="L1", ratio=0.1, reveals_rubric_items=["R9"]),
    ]
    with pytest.raises(ValidationError):
        TaskSpec(**_base_kwargs(hint_ladder=bad_hints))
