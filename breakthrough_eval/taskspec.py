"""Load & validate TaskSpecs from YAML (plan §2).

Adding a new "XXX 测试" = drop a YAML file that passes ``validate_taskspec``;
no code changes. The hard red-line invariants are enforced by the pydantic model
validator; this module adds friendly cross-checks + bulk loading.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import TaskSpec


def load_taskspec(path: str | Path) -> TaskSpec:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return TaskSpec.model_validate(data)


def load_all_tasks(directory: str | Path) -> dict[str, TaskSpec]:
    tasks: dict[str, TaskSpec] = {}
    for p in sorted(Path(directory).glob("*.y*ml")):
        spec = load_taskspec(p)
        if spec.task_id in tasks:
            raise ValueError(f"重复 task_id: {spec.task_id} ({p})")
        tasks[spec.task_id] = spec
    return tasks


def validate_taskspec(path: str | Path) -> list[str]:
    """Return a list of human-readable issues ([] means valid)."""
    issues: list[str] = []
    try:
        spec = load_taskspec(path)
    except (ValidationError, ValueError) as exc:
        return [str(exc)]

    # Soft / advisory checks beyond the hard invariants.
    if not spec.contamination_probes:
        issues.append("⚠️ 没有定义任何污染探针 (contamination_probes): 无法执行 probe-then-prove。")
    if spec.max_hint_level < 1:
        issues.append("⚠️ hint_ladder 只有 L0: 拿不到难度曲线 (建议补 L1..Lk)。")
    delta_items = [r for r in spec.rubric if r.frontier_delta]
    if not delta_items:
        issues.append("⚠️ rubric 里没有任何 frontier_delta=True 的关键创新点。")
    for item in spec.rubric:
        if not item.indicators:
            issues.append(f"⚠️ rubric {item.id} 没有 indicators: mock 评委无法机检该项。")
    framing = (spec.problem_statement + " " + spec.problem_framing_notes).lower()
    flagged: set[str] = set()
    for probe in spec.contamination_probes:
        for ind in probe.leak_indicators:
            if ind.lower() in framing and ind.lower() not in flagged:
                flagged.add(ind.lower())
                issues.append(
                    f"⚠️ 题面疑似泄露探针关键词 '{ind}' "
                    "(problem_statement/problem_framing_notes 不应指向答案)。"
                )
    return issues
