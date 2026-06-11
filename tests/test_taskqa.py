"""Task QA — task 本身的单元测试 (生产流水线验收门)."""

import json

from breakthrough_eval.contamination import SemanticProbeJudge
from breakthrough_eval.eval.evaluator import Evaluator
from breakthrough_eval.eval.mock import MockJudge, MockJudgeConfig
from breakthrough_eval.models import ContaminationProbe, RubricItem
from breakthrough_eval.taskqa import run_task_qa


def _panel():
    return Evaluator([
        MockJudge(MockJudgeConfig(strictness=0.4, name="lenient")),
        MockJudge(MockJudgeConfig(strictness=0.7, name="strict")),
    ])


def test_real_task_passes_qa(task):
    report = run_task_qa(task, _panel())
    assert report.passed, report.render()
    assert "golden_anchoring" in report.render()


def test_dead_probe_threshold_caught(task):
    # threshold > 可用指示词数 → 探针永远不会触发 → hard fail。
    bad = task.model_copy(deep=True)
    bad.contamination_probes[0] = ContaminationProbe(
        id="P_dead", kind="direct", prompt="q",
        leak_indicators=["某关键创新"], threshold=2,
    )
    report = run_task_qa(bad, _panel())
    assert not report.passed
    assert any("probe_triggerable[P_dead]" == c.name for c in report.hard_failures)


def test_overbroad_indicator_caught(task):
    # 指示词出现在题面里 → 复述题面就会被冤杀 → hard fail。
    bad = task.model_copy(deep=True)
    bad.contamination_probes[0] = ContaminationProbe(
        id="P_broad", kind="direct", prompt="q",
        leak_indicators=["Kakeya"], threshold=1,
    )
    report = run_task_qa(bad, _panel())
    assert any("probe_clean_pass[P_broad]" == c.name for c in report.hard_failures)


def test_unanchorable_rubric_caught(task):
    # rubric item 无 indicators → mock 评委金标也判不过 → golden 锚定 fail。
    bad = task.model_copy(deep=True)
    bad.rubric[0] = RubricItem(id="R1", title="无标志项", criterion="c", indicators=[])
    report = run_task_qa(bad, _panel())
    assert any(c.name == "golden_anchoring" for c in report.hard_failures)


def test_semantic_calibration_detects_golden_and_passes_clean(task):
    def fake_complete(system, user):
        # golden 概要含「证明结构概要」字样 → 判泄露; 诚实回答 → clean。
        leaked = "证明结构概要" in user
        return json.dumps({"leaked": leaked, "evidence": "fake", "confidence": 0.9})

    judge = SemanticProbeJudge(complete=fake_complete)
    report = run_task_qa(task, _panel(), probe_judge=judge)
    assert report.passed, report.render()
    names = [c.name for c in report.checks]
    assert any(n.startswith("semantic_detects_golden") for n in names)
    assert "semantic_clean_pass" in names


def test_semantic_misfire_on_clean_caught(task):
    judge = SemanticProbeJudge(  # 永远喊泄露的失调评审 → clean_pass 必须抓住
        complete=lambda s, u: json.dumps({"leaked": True, "evidence": "x"}))
    report = run_task_qa(task, _panel(), probe_judge=judge)
    assert any(c.name == "semantic_clean_pass" for c in report.hard_failures)


def test_cli_task_qa(tmp_path, capsys):
    from pathlib import Path

    from breakthrough_eval.cli import main

    ROOT = Path(__file__).resolve().parent.parent
    rc = main([
        "--tasks-dir", str(ROOT / "tasks"),
        "--registry", str(ROOT / "models_registry.yaml"),
        "--results-dir", str(tmp_path / "results"),
        "task-qa",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "✅ 通过" in out
