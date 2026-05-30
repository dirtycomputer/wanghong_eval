"""LLM-as-judge backend (plan §4.2) — pluggable strong-judge stub.

EVAL is allowed to use the strongest closed API. To stay provider-neutral and
offline-safe, this backend takes a ``complete(system, user) -> str`` callable
that must return JSON. No network client is imported here; wire in an Anthropic
/ OpenAI client at call time. Without a client it degrades gracefully.

The prompt enforces the §4.2 reliability rules: per-item verdict, **line
citations required**, no blanket judgement.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from ..models import GoldenProof, JudgeVerdict, RubricItemVerdict, TaskSpec
from .base import JudgeBackend

CompleteFn = Callable[[str, str], str]

JUDGE_SYSTEM = """\
你是一名严格的数学评审。你被允许知道 golden proof, 但你的职责不是比对措辞,
而是判断 prover 的论证是否**真的成立且达成同一定理**。golden proof 仅作对齐参照;
若 prover 走了一条不同但有效的路径 (alternative-but-valid), 也应判其有效。

规则:
- 逐条 rubric item 判定 pass/fail, 并给出 prover_output 的具体行号作为证据 (禁止笼统判断)。
- 对自信的胡话保持警惕: 没有可核查的论证就判 fail。
- justification 必须简短 (每条 <= 40 字), 以免输出超长被截断。
- 只输出一个 JSON 对象, 不要 markdown 代码块、不要任何解释性文字。
"""


def build_judge_prompt(task: TaskSpec, proof_text: str, golden: GoldenProof) -> str:
    numbered = "\n".join(f"{i}: {ln}" for i, ln in enumerate(proof_text.splitlines(), 1))
    rubric_lines = "\n".join(
        f"- {r.id}: {r.title} — 判定标准: {r.criterion}" for r in task.rubric
    )
    schema = (
        '{"item_verdicts": [{"item_id": "R1", "passed": true, '
        '"justification": "...", "cited_lines": [3,4], "confidence": 0.8}], '
        '"overall_valid": false, "alternative_valid": false, "notes": "..."}'
    )
    return (
        f"# 定理\n{task.problem_statement}\n\n"
        f"# Golden proof (仅对齐参照)\n{golden.primary}\n"
        + ("companions: " + "; ".join(golden.companions) + "\n" if golden.companions else "")
        + f"\n# Rubric (逐条判定)\n{rubric_lines}\n\n"
        f"# Prover 产物 (带行号)\n{numbered}\n\n"
        f"# 输出 JSON schema\n{schema}\n"
    )


class LLMJudge(JudgeBackend):
    def __init__(
        self,
        complete: Optional[CompleteFn] = None,
        name: str = "llm-judge",
        max_parse_retries: int = 1,
    ):
        self._complete = complete
        self.name = name
        self.max_parse_retries = max_parse_retries

    def available(self) -> bool:
        return self._complete is not None

    def judge(self, task: TaskSpec, proof_text: str, golden: GoldenProof) -> JudgeVerdict:
        if self._complete is None:
            raise RuntimeError(
                "LLMJudge 未配置 complete() 回调: 请注入一个返回 JSON 的强评委 API "
                "(本环境请改用 MockJudge)。"
            )
        user = build_judge_prompt(task, proof_text, golden)
        last_err = ""
        for _ in range(self.max_parse_retries + 1):
            try:
                raw = self._complete(JUDGE_SYSTEM, user)
                return self._parse(raw, task)
            except Exception as exc:  # noqa: BLE001
                # JSON 截断 / 非法输出 / 网络错误: 重试一次, 仍失败则弃权进人工复核
                # (plan §4.2)。一票评委的瞬时失败不应中断整轮 (尤其并行时)。
                last_err = f"{type(exc).__name__}: {exc}"
        return JudgeVerdict(
            judge_name=self.name,
            item_verdicts=[],
            overall_valid=False,
            notes=f"评委输出无法解析为 JSON, 弃权: {last_err}",
            parse_failed=True,
        )

    def _parse(self, raw: str, task: TaskSpec) -> JudgeVerdict:
        data = json.loads(_extract_json(raw))
        verdicts = [
            RubricItemVerdict(
                item_id=v["item_id"],
                passed=bool(v.get("passed", False)),
                justification=v.get("justification", ""),
                cited_lines=list(v.get("cited_lines", [])),
                confidence=float(v.get("confidence", 0.5)),
            )
            for v in data.get("item_verdicts", [])
        ]
        return JudgeVerdict(
            judge_name=self.name,
            item_verdicts=verdicts,
            overall_valid=bool(data.get("overall_valid", False)),
            alternative_valid=bool(data.get("alternative_valid", False)),
            notes=data.get("notes", ""),
        )


def _extract_json(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"评委输出里找不到 JSON: {raw[:200]!r}")
    return raw[start : end + 1]
