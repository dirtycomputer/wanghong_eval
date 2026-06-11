"""Deterministic mock judge (default, offline).

Judges each rubric item by whether enough of its ``indicators`` appear in the
proof text, with a per-judge ``strictness`` knob so that a panel of judges can
*disagree* on partially-covered items — that disagreement is what feeds Cohen's
κ / the human-review queue (plan §4.2). It also cites the line numbers where
evidence was found (plan §4.2: 禁止笼统判断).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import GoldenProof, JudgeVerdict, RubricItemVerdict, TaskSpec
from .base import JudgeBackend


@dataclass
class MockJudgeConfig:
    strictness: float = 0.5  # fraction of an item's indicators required to pass
    name: str = "mock-judge"


class MockJudge(JudgeBackend):
    def __init__(self, config: MockJudgeConfig | None = None):
        self.config = config or MockJudgeConfig()
        self.name = self.config.name

    def describe(self) -> dict:
        return {"name": self.name, "kind": "MockJudge", "strictness": self.config.strictness}

    def judge(self, task: TaskSpec, proof_text: str, golden: GoldenProof) -> JudgeVerdict:
        lines = proof_text.splitlines()
        low_lines = [ln.lower() for ln in lines]

        verdicts: list[RubricItemVerdict] = []
        delta_pass = True
        for item in task.rubric:
            indicators = item.indicators or []
            present_lines: list[int] = []
            present = 0
            for ind in indicators:
                il = ind.lower()
                for i, ln in enumerate(low_lines, start=1):
                    if il in ln:
                        present += 1
                        present_lines.append(i)
                        break
            need = max(1, math.ceil(self.config.strictness * len(indicators))) if indicators else 1
            passed = bool(indicators) and present >= need
            if item.frontier_delta and not passed:
                delta_pass = False
            verdicts.append(
                RubricItemVerdict(
                    item_id=item.id,
                    passed=passed,
                    justification=(
                        f"匹配 {present}/{len(indicators)} 个关键标志 (阈值 {need})。"
                        if indicators
                        else "该 item 无可机检标志, 判未通过。"
                    ),
                    cited_lines=sorted(set(present_lines)),
                    confidence=min(1.0, 0.5 + 0.1 * present),
                )
            )

        # 闭合判据: 所有 frontier-delta 关键创新都触及才算整体有效证明。
        overall_valid = delta_pass and any(r.frontier_delta for r in task.rubric)
        return JudgeVerdict(
            judge_name=self.name,
            item_verdicts=verdicts,
            overall_valid=overall_valid,
            alternative_valid=False,
            notes="mock 评委: 基于 rubric 标志词 + 严格度阈值的可复现判定。",
        )


def _factory(**kwargs) -> MockJudge:
    return MockJudge(
        MockJudgeConfig(
            strictness=kwargs.get("strictness", 0.5),
            name=kwargs.get("name", "mock-judge"),
        )
    )
