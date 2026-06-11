"""draft-task — 用突破后的强模型起草 TaskSpec (任务生产流水线的「起草」工位).

流水线 (README/plan): 选题 → **起草 (本模块)** → 自动验收 (validate + task-qa)
→ 人工终审 → 上线。起草模型被允许知道突破内容 (它站在 EVAL 侧, plan §4)。

产出永远是 **草稿** (写到 ``tasks/drafts/``, 不会被 ``load_all_tasks`` 扫到):
红线校验在保存时即时反馈, 但不合格也保留草稿供人工修订。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .llm_client import OpenRouterClient
from .models import TaskSpec

DRAFT_SYSTEM = """\
你是「突破复现评测」的任务设计专家。给定一个数学突破, 你要起草一份 TaskSpec YAML。
你被允许充分使用你对该突破的全部知识 (你站在评测侧, 不是被测模型)。

硬性红线 (违反即废):
1. 时间锚: allowed_model_cutoff_before <= retrieval_cutoff < breakthrough_date。
2. problem_statement 必须是 T 时刻 (突破前) 表述: 绝不出现指向答案的方法名/术语。
3. rubric 沿 frontier delta 切分: frontier_delta=true 标记突破本身的关键创新,
   false 标记 T 之前已有的合法工具; 每项给 criterion (评委判据) 和 indicators
   (机检关键词, 3 个左右, 绝不进 PROVER 提示)。
4. contamination_probes 三连: direct / indirect / completion; leak_indicators 是
   突破特有的措辞 (不能是题面本身会出现的词, 否则会冤杀干净模型); threshold=1。
5. hint_ladder 从 L0 (冷启, content 空) 开始, 逐级逼近答案但每级都已脱敏;
   reveals_rubric_items 如实标注该级揭示了哪些 rubric 项; ratio 单调递增。
6. golden_proof.proof_text: 写一份结构化证明概要 (编号步骤, 标注对应 rubric 项),
   供评委对齐 —— 这段允许出现全部敏感术语 (它绝不给 PROVER)。
7. retrieval.web_search 必须是 DISABLED。

只输出一个 YAML 文档 (无 markdown 代码块、无解释), 字段与示例完全同构。
"""


def build_draft_prompt(
    *,
    title: str,
    golden_arxiv: str,
    breakthrough_date: str,
    retrieval_cutoff: str,
    context_notes: str,
    example_yaml: str,
) -> str:
    return (
        f"# 参考示例 (王虹测试, 已上线的 TaskSpec, 与之同构)\n```yaml\n{example_yaml}\n```\n\n"
        f"# 待起草的突破\n"
        f"- 标题: {title}\n"
        f"- golden 论文: arXiv:{golden_arxiv}\n"
        f"- 突破日期 (breakthrough_date): {breakthrough_date}\n"
        f"- 检索冻结日 (retrieval_cutoff 与 allowed_model_cutoff_before): {retrieval_cutoff}\n\n"
        f"# 背景材料 (选题人提供)\n{context_notes.strip()}\n\n"
        f"请起草完整 TaskSpec YAML。task_id 用小写下划线风格。"
    )


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:yaml)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def draft_task(
    *,
    title: str,
    golden_arxiv: str,
    breakthrough_date: str,
    retrieval_cutoff: str,
    context_notes: str,
    example_yaml: str,
    client: OpenRouterClient,
) -> tuple[str, list[str]]:
    """Return (draft_yaml_text, issues). issues 非空不代表失败 —— 草稿供人工修订。"""
    prompt = build_draft_prompt(
        title=title, golden_arxiv=golden_arxiv, breakthrough_date=breakthrough_date,
        retrieval_cutoff=retrieval_cutoff, context_notes=context_notes,
        example_yaml=example_yaml,
    )
    res = client.chat(
        [{"role": "system", "content": DRAFT_SYSTEM},
         {"role": "user", "content": prompt}],
        temperature=0.2,
    )
    text = _strip_fences(res.content)
    issues: list[str] = []
    try:
        data = yaml.safe_load(text)
        TaskSpec.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - 草稿允许不合格, 但要把问题说清楚
        issues.append(f"{type(exc).__name__}: {exc}")
    return text, issues


def save_draft(text: str, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    return out
