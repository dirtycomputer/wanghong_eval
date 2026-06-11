"""Task QA — task 本身的单元测试 (任务生产流水线的自动验收门).

一个 TaskSpec 在上线前必须通过:

  * **golden 锚定** (plan §4.2): 金标证明走完整评委管线必须拿满分且整体有效 ——
    拿不到说明 rubric 切分或评委失调, 是 task 的问题不是模型的问题;
  * **探针可触发** (plan §8.3): 用 leak_indicators 合成的泄露样本必须触发关键词层
    (抓 threshold > 可用指示词、空指示词等配置错误 —— 这种探针永远不会报警);
  * **探针不误杀**: 「诚实不知道 + 复述题面」的回答绝不能触发 —— 抓过宽的指示词
    (例如把题面本身的词当泄露词, 会把所有模型都冤杀);
  * **语义层校准** (可选, 需 probe_judge): 把 golden 证明概要当作答案喂给探针,
    语义评审必须报警 (它就是「知道答案的人」会说的话); 诚实回答必须放行;
  * **hint 阶梯完整性** (advisory): 每个 rubric item 应被某级揭示 (否则阶梯到顶也
    给不出方向), ratio 单调不减。

CLI: ``python -m breakthrough_eval task-qa [--task id] [--judges ...] [--probe-judge ...]``
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contamination import evaluate_probe
from .eval.evaluator import Evaluator
from .models import TaskSpec

HONEST_CLEAN_ANSWER = (
    "据我所知, 这是一个公认的公开未解决问题。我不清楚是否已有完整证明, "
    "也无法凭空给出突破性的解法。"
)


@dataclass
class QACheck:
    name: str
    passed: bool
    hard: bool  # hard 失败 → task 不可上线; soft 仅提示
    detail: str = ""


@dataclass
class TaskQAReport:
    task_id: str
    checks: list[QACheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.hard)

    @property
    def hard_failures(self) -> list[QACheck]:
        return [c for c in self.checks if c.hard and not c.passed]

    def render(self) -> str:
        lines = [f"Task QA — {self.task_id}: {'✅ 通过' if self.passed else '❌ 未通过'}"]
        for c in self.checks:
            mark = "✅" if c.passed else ("❌" if c.hard else "⚠️")
            lines.append(f"  {mark} {c.name}: {c.detail}")
        return "\n".join(lines)


def run_task_qa(task: TaskSpec, evaluator: Evaluator, probe_judge=None) -> TaskQAReport:
    report = TaskQAReport(task_id=task.task_id)
    add = report.checks.append

    # 1. Golden 锚定: 金标证明必须在评委管线里拿满分 (plan §4.2)。
    res = evaluator.calibrate_with_golden(task)
    add(QACheck(
        "golden_anchoring",
        res.passed_items == res.total_items and res.overall_valid,
        hard=True,
        detail=(f"金标 {res.passed_items}/{res.total_items} items, "
                f"overall={res.overall_valid}, κ={res.agreement:.2f}"
                + (" [进人工复核]" if res.needs_human_review else "")),
    ))

    # 2. 探针关键词层: 合成泄露样本必须触发 (抓配置死探针)。
    for probe in task.contamination_probes:
        leak_text = "; ".join(probe.leak_indicators)
        pr = evaluate_probe(probe, leak_text)
        add(QACheck(
            f"probe_triggerable[{probe.id}]", pr.leaked, hard=True,
            detail=f"threshold {probe.threshold}/{len(probe.leak_indicators)} 个指示词, "
                   f"合成样本命中 {len(pr.matched_indicators)} 个",
        ))

    # 3. 探针不误杀: 诚实不知道 + 复述题面 绝不能触发。
    clean_text = HONEST_CLEAN_ANSWER + "\n" + task.problem_statement
    for probe in task.contamination_probes:
        pr = evaluate_probe(probe, clean_text)
        add(QACheck(
            f"probe_clean_pass[{probe.id}]", not pr.leaked, hard=True,
            detail="诚实回答+题面复述不应触发"
                   + (f"; 误命中={pr.matched_indicators}" if pr.matched_indicators else ""),
        ))

    # 4. 语义层校准 (可选): golden 概要 = 「知道答案的人」的回答, 必须被抓;
    #    诚实回答必须放行。
    if probe_judge is not None:
        leak_sample = task.golden_proof.proof_text.strip()
        if not leak_sample:
            add(QACheck("semantic_calibration", False, hard=False,
                        detail="golden_proof.proof_text 为空, 无法做语义层校准"))
        else:
            for probe in task.contamination_probes:
                leaked, notes = probe_judge.assess(task, probe, leak_sample)
                add(QACheck(
                    f"semantic_detects_golden[{probe.id}]", leaked, hard=True,
                    detail=notes[:140],
                ))
            leaked, notes = probe_judge.assess(
                task, task.contamination_probes[0], clean_text
            )
            add(QACheck("semantic_clean_pass", not leaked, hard=True, detail=notes[:140]))

    # 5. hint 阶梯完整性 (advisory)。
    revealed_all: set[str] = set()
    for h in task.hint_ladder:
        revealed_all.update(h.reveals_rubric_items)
    never = [r.id for r in task.rubric if r.id not in revealed_all]
    add(QACheck("hint_ladder_reveals_all", not never, hard=False,
                detail=f"从未被任何 hint 揭示: {never}" if never else "全部 item 可被阶梯揭示"))
    ratios = [h.ratio for h in task.hint_ladder]
    add(QACheck("hint_ratio_monotonic", ratios == sorted(ratios), hard=False,
                detail=f"ratios={ratios}"))

    return report
