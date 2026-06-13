"""Hint ladder & prompt assembly (plan §5).

Turns the binary "做没做出来" into a continuous "需要多少脚手架" curve.

Critical safeguard (plan §10 risk 4): the **rubric wording never enters the
PROVER prompt**. Only ``problem_statement`` + the cumulative ``hint_ladder``
content (already human-desensitised in the spec) is shown. This module is the
single choke point that builds prover-facing text, so the invariant is easy to
keep.
"""

from __future__ import annotations

from ..specification import ProbeKind, TaskSpec

PROBE_SYSTEM_PROMPT = """\
你是一名数学研究者。请基于你已有的知识, 简短、直接地回答下面的问题 (几句话即可)。
不要写长篇证明; 如果你不确定或不知道, 就直说。
"""

PROVER_SYSTEM_PROMPT = """\
你是一名数学研究者, 正在参加一场"时间冻结的开卷研究考试"。
- 你只能使用截止到给定日期之前公开的技巧、引理与工具。
- 你可以使用 restricted_search 检索工具; 它只返回当前评测 cutoff 之前的搜索结果, 不得使用其它联网检索。
- 你必须尝试构造证明, 给出你能给出的最完整、最严谨的证明路线。
- 不要把"这是公开未解决问题"、"我不能证明"或类似表述作为最终答案。
- 如果某一步无法闭合, 仍要写出你尝试使用的引理、归纳框架、关键估计和具体缺口。
- 可以诚实标注不确定或未能闭合的步骤, 但不能只停留在拒绝证明或背景综述。

请严格按以下结构输出 (这是评测对齐所必需的):

# Claimed Proof
## 1. 思路总览
## 2. 关键引理与论证
## 3. 完整证明
## 4. 我引用/检索到的文献清单
"""


def prove_prompt(task: TaskSpec, hint_level: int) -> str:
    """Assemble the prove-phase user prompt for a given hint level.

    Hints are *cumulative*: level N includes the content of all levels <= N.
    """
    chosen = [h for h in task.hint_ladder if h.level <= hint_level]
    if not chosen or chosen[-1].level != hint_level:
        raise KeyError(f"task {task.task_id} 没有 hint level {hint_level}")

    parts = [f"# 问题\n{task.problem_statement.strip()}"]

    hint_blocks = [h.content.strip() for h in chosen if h.content.strip()]
    if hint_blocks:
        parts.append("# 允许使用的提示 (越往下越接近答案)")
        for h in chosen:
            if h.content.strip():
                parts.append(f"## {h.label} (~{int(h.ratio * 100)}%)\n{h.content.strip()}")
    else:
        parts.append("# 提示\n(本级为冷启, 无额外提示)")

    parts.append(
        "# 检索说明\n你可以调用 restricted_search 检索工具。它只会返回当前评测 cutoff 之前的结果。"
        "请在第 4 节如实列出你实际用到的文献。"
    )
    return "\n\n".join(parts)


def probe_prompt(task: TaskSpec, probe_id: str) -> str:
    """Return the raw text of a contamination probe (plan §3.3, §8.3)."""
    for p in task.contamination_probes:
        if p.id == probe_id:
            return p.prompt
    raise KeyError(f"task {task.task_id} 没有探针 {probe_id}")


def probe_kind(task: TaskSpec, probe_id: str) -> ProbeKind:
    for p in task.contamination_probes:
        if p.id == probe_id:
            return p.kind
    raise KeyError(f"task {task.task_id} 没有探针 {probe_id}")
