"""Core data models for the breakthrough-reproduction evaluation system.

Everything in this system is driven by a *pure-data* ``TaskSpec`` (plan §2): the
code is isomorphic across all tasks, so adding a new "XXX 测试" means adding a
spec + passing validation, not changing code.

The single make-or-break constraint of the whole benchmark (plan §0, §8) is
encoded directly on ``TaskSpec``:

    allowed_model_cutoff_before <= retrieval_cutoff < breakthrough_date

If that ordering is violated the task is rejected before any job is scheduled.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --------------------------------------------------------------------------- #
# TaskSpec sub-models
# --------------------------------------------------------------------------- #
class GoldenProof(BaseModel):
    """The reference proof. Given only to EVAL, never to PROVER (plan §2)."""

    primary: str = Field(..., description="一手论文 (e.g. 'arXiv:2502.17655 (Wang, Zahl)').")
    companions: list[str] = Field(
        default_factory=list,
        description="辅助 EVAL / rubric 构建的二手源 (综述、博客解读等).",
    )
    proof_text: str = Field(
        "",
        description="证明全文或结构化概要, 注入评委 prompt 做对齐 (绝不给 PROVER)。"
        "缺省时评委只能凭自身训练知识判定 —— 对冷门突破会失真, validate 会警告.",
    )


class RubricItem(BaseModel):
    """One independently-judgeable key ingredient of the breakthrough (plan §4.1).

    ``frontier_delta=True`` marks the *contested* innovation that the model must
    cross on its own (vs. legal pre-T tools, which are ``frontier_delta=False``).
    """

    id: str = Field(..., description="稳定标识, e.g. 'R1'.")
    title: str
    criterion: str = Field(..., description="判定标准 (评委据此逐条判 PROVER 是否触及).")
    frontier_delta: bool = Field(
        True,
        description="是否属于突破本身的关键创新 (要测模型能否自己跨过的 delta).",
    )
    # Indicators are used by the *mock* judge / contamination matcher only.
    # They are NEVER injected into the PROVER prompt (plan §10 risk 4).
    indicators: list[str] = Field(
        default_factory=list,
        description="该 item 在证明文本里典型会出现的关键词 (供 mock 评委/探针匹配, 不进 PROVER 提示).",
    )


class ProbeKind(str, Enum):
    """Contamination probe styles (plan §8.3)."""

    DIRECT = "direct"  # 直接问「被证明了吗?怎么证的?」
    INDIRECT = "indirect"  # 间接问, 看它对答案出处是否异常熟悉
    COMPLETION = "completion"  # 给开头看它能否续写出突破特有结构


class ContaminationProbe(BaseModel):
    """A single leakage probe run *before* the real task (plan §3.3, §8.3)."""

    id: str
    kind: ProbeKind
    prompt: str
    leak_indicators: list[str] = Field(
        ...,
        description="命中即视为泄露关键创新的措辞 (Wang–Zahl 特有结构等).",
    )
    threshold: int = Field(
        1, ge=1, description="命中多少个 indicator 才判泄露 (默认 1).")


class HintLevel(BaseModel):
    """One rung of the hint ladder (plan §5)."""

    level: int = Field(..., ge=0, description="0 = 冷启 (L0).")
    label: str = Field(..., description="展示名, e.g. 'L0'.")
    ratio: float = Field(..., ge=0.0, le=1.0, description="大致提示比例 (0%→~90%).")
    content: str = Field("", description="该级附加的提示文本 (L0 通常为空).")
    reveals_rubric_items: list[str] = Field(
        default_factory=list,
        description="该级提示揭示了哪些 rubric item 的方向 (仅作审计记录).",
    )


class RetrievalConfig(BaseModel):
    """PROVER 的检索面 (plan §2)。web search 红线默认关闭。"""

    arxiv_snapshot: str = Field("frozen <= retrieval_cutoff")
    other_sources: list[str] = Field(default_factory=list)
    web_search: Literal["DISABLED", "ENABLED"] = "DISABLED"


# --------------------------------------------------------------------------- #
# TaskSpec
# --------------------------------------------------------------------------- #
class TaskSpec(BaseModel):
    """A breakthrough task — the scale-up bedrock (plan §2)."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    domain: str

    # —— 时间锚 (决定一切污染防控) ——
    breakthrough_date: date
    retrieval_cutoff: date
    allowed_model_cutoff_before: date

    # —— 题面 (必须是 T 时刻表述, 不能泄露后续) ——
    problem_statement: str
    problem_framing_notes: str = ""

    # —— golden proof (只给 EVAL) ——
    golden_proof: GoldenProof

    # —— rubric / 探针 / hint 阶梯 ——
    rubric: list[RubricItem] = Field(..., min_length=1)
    contamination_probes: list[ContaminationProbe] = Field(default_factory=list)
    hint_ladder: list[HintLevel] = Field(..., min_length=1)

    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)

    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _check_invariants(self) -> "TaskSpec":
        # The red-line ordering (plan §2, §8).
        if not (
            self.allowed_model_cutoff_before
            <= self.retrieval_cutoff
            < self.breakthrough_date
        ):
            raise ValueError(
                "时间锚违反红线: 必须满足 "
                "allowed_model_cutoff_before <= retrieval_cutoff < breakthrough_date "
                f"(得到 {self.allowed_model_cutoff_before} / "
                f"{self.retrieval_cutoff} / {self.breakthrough_date})."
            )

        # Unique rubric ids.
        rubric_ids = [r.id for r in self.rubric]
        if len(rubric_ids) != len(set(rubric_ids)):
            raise ValueError(f"rubric item id 重复: {rubric_ids}")

        # Hint ladder must start at L0 and be strictly increasing in level.
        levels = [h.level for h in self.hint_ladder]
        if levels[0] != 0:
            raise ValueError("hint_ladder 必须从 level 0 (冷启) 开始.")
        if levels != sorted(levels) or len(levels) != len(set(levels)):
            raise ValueError(f"hint_ladder level 必须严格递增且唯一: {levels}")

        # Hints may only reference real rubric items.
        valid_ids = set(rubric_ids)
        for h in self.hint_ladder:
            unknown = set(h.reveals_rubric_items) - valid_ids
            if unknown:
                raise ValueError(
                    f"hint {h.label} 引用了不存在的 rubric item: {sorted(unknown)}"
                )

        # web search red-line.
        if self.retrieval.web_search != "DISABLED":
            raise ValueError("retrieval.web_search 必须为 DISABLED (污染防控红线).")
        return self

    # Convenience -------------------------------------------------------- #
    @property
    def max_hint_level(self) -> int:
        return self.hint_ladder[-1].level

    def hint(self, level: int) -> HintLevel:
        for h in self.hint_ladder:
            if h.level == level:
                return h
        raise KeyError(f"task {self.task_id} 没有 hint level {level}")

    def rubric_item(self, item_id: str) -> RubricItem:
        for r in self.rubric:
            if r.id == item_id:
                return r
        raise KeyError(f"task {self.task_id} 没有 rubric item {item_id}")


# --------------------------------------------------------------------------- #
# Model registry (plan §6.2)
# --------------------------------------------------------------------------- #
class ModelEntry(BaseModel):
    """A candidate PROVER model with a (soft!) training cutoff."""

    name: str
    cutoff_date: date
    cutoff_confidence: Literal["low", "medium", "high"] = "medium"
    open_source: bool = False
    capability_tier: str = Field("unknown", description="能力档位标签, e.g. 'research'.")
    provider: str = Field("mock", description="后端选择键 (mock / local-vllm / ...).")
    backend_kwargs: dict = Field(
        default_factory=dict,
        description="传给后端 factory 的参数 (mock: capability/contaminated/scaffold_version; "
        "codex: model/model_provider). provider-agnostic.",
    )

    def eligible_for(self, task: TaskSpec) -> bool:
        """Eligible iff the model's training cutoff is within the task's allowed
        window — i.e. no later than ``allowed_model_cutoff_before`` (plan §2, §6.2).

        Uses the dedicated admission threshold rather than ``retrieval_cutoff``
        (the two coincide by default, but the threshold is what governs model
        admission). ``<=`` so a model sitting exactly on the boundary — e.g. a
        documented "January 2025" cutoff against a 2025-01-31 threshold — is
        admitted; the §8 probes remain the final arbiter of cleanliness.
        """
        return self.cutoff_date <= task.allowed_model_cutoff_before


# --------------------------------------------------------------------------- #
# Jobs (plan §1: job = task × model × hint_level × trial)
# --------------------------------------------------------------------------- #
class Job(BaseModel):
    task_id: str
    model: str
    hint_level: int
    trial: int

    @property
    def job_id(self) -> str:
        return f"{self.task_id}::{self.model}::L{self.hint_level}::t{self.trial}"


# --------------------------------------------------------------------------- #
# PROVER artifacts (plan §3.4)
# --------------------------------------------------------------------------- #
class ProverStructuredOutput(BaseModel):
    """The structured writeup PROVER is required to produce (plan §3.4)."""

    idea_overview: str = ""
    key_lemmas: str = ""
    full_proof: str = ""
    cited_references: list[str] = Field(default_factory=list)

    def as_text(self) -> str:
        return "\n\n".join(
            [
                "# Claimed Proof",
                "## 1. 思路总览\n" + self.idea_overview,
                "## 2. 关键引理与论证\n" + self.key_lemmas,
                "## 3. 完整证明\n" + self.full_proof,
                "## 4. 引用文献\n" + "\n".join(f"- {r}" for r in self.cited_references),
            ]
        )


class ProbeResponse(BaseModel):
    probe_id: str
    kind: ProbeKind
    response_text: str
    matched_indicators: list[str] = Field(default_factory=list)
    leaked: bool = False  # 最终裁决 (关键词命中 或 语义评审命中)
    semantic_leak: bool = Field(
        False, description="语义评审判定的实质复现 (抓关键词之外的换措辞泄露).")
    semantic_notes: str = Field(
        "", description="语义评审的证据/弃权说明 (审计留痕).")


class ToolCall(BaseModel):
    """A recorded tool invocation from the transcript (for audit, plan §3.2/§8.4)."""

    tool: str
    query: str = ""
    host: str = ""
    returned_dates: list[date] = Field(default_factory=list)


class AuditResult(BaseModel):
    """Post-hoc transcript audit (plan §8.4)."""

    out_of_window_references: list[str] = Field(default_factory=list)
    out_of_window_network: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.out_of_window_references and not self.out_of_window_network


class UsageStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    wall_seconds: float = 0.0

    @property
    def cost_units(self) -> float:
        # Abstract cost; real backends override with $/token.
        return (self.input_tokens + self.output_tokens) / 1000.0


class ProverRunResult(BaseModel):
    """Everything one PROVER run produces for a single Job."""

    job_id: str
    task_id: str
    model: str
    hint_level: int
    trial: int
    harness: str = Field(..., description="指纹: model + scaffold + toolset (plan §7).")

    contaminated: bool = False
    probe_responses: list[ProbeResponse] = Field(default_factory=list)

    structured_output: Optional[ProverStructuredOutput] = None
    raw_output: str = ""
    transcript: list[ToolCall] = Field(default_factory=list)

    audit: AuditResult = Field(default_factory=AuditResult)
    usage: UsageStats = Field(default_factory=UsageStats)
    error: Optional[str] = None

    @property
    def proof_text(self) -> str:
        if self.structured_output is not None:
            return self.structured_output.as_text()
        return self.raw_output

    @property
    def excluded(self) -> bool:
        """Excluded from scoring iff contaminated or audit failed (plan §7, §8)."""
        return self.contaminated or not self.audit.passed


# --------------------------------------------------------------------------- #
# EVAL artifacts (plan §4)
# --------------------------------------------------------------------------- #
class RubricItemVerdict(BaseModel):
    item_id: str
    passed: bool
    justification: str = ""
    cited_lines: list[int] = Field(
        default_factory=list, description="prover_output 行号 (禁止笼统判断, plan §4.2)."
    )
    confidence: float = Field(0.5, ge=0.0, le=1.0)


class JudgeVerdict(BaseModel):
    judge_name: str
    item_verdicts: list[RubricItemVerdict]
    overall_valid: bool = False
    alternative_valid: bool = Field(
        False, description="评委判定的 alternative-but-valid 另解 (plan §4.1, §10.3)."
    )
    notes: str = ""
    parse_failed: bool = Field(
        False,
        description="评委输出无法解析 (e.g. JSON 截断). 该评委视为弃权并进人工复核.",
    )

    def verdict_map(self) -> dict[str, bool]:
        return {v.item_id: v.passed for v in self.item_verdicts}


class EvalResult(BaseModel):
    """Aggregated multi-judge verdict for one Job (plan §4.2, §4.3)."""

    job_id: str
    task_id: str
    judges: list[JudgeVerdict] = Field(default_factory=list)

    # Consensus per rubric item (majority vote).
    item_consensus: dict[str, bool] = Field(default_factory=dict)
    passed_items: int = 0
    total_items: int = 0
    overall_valid: bool = False
    alternative_valid: bool = False

    agreement: float = Field(1.0, description="评委一致性 (平均 pairwise Cohen's κ).")
    needs_human_review: bool = False
    excluded: bool = False  # mirrors ProverRunResult.excluded
    errored: bool = Field(
        False,
        description="基础设施错误 (网络/后端异常) 的作废 run: 不评、不计入 solve_rate (≠ 模型没解出来).",
    )

    # earned vs given (plan §5): hint 已揭示的 item 不算模型「挣到」的。
    revealed_items: list[str] = Field(
        default_factory=list,
        description="该 hint 级 (累积 ≤level) 已向 PROVER 揭示方向的 rubric items.",
    )
    earned_passed_items: int = 0
    earned_total_items: int = Field(
        0, description="未被 hint 揭示的 item 数 (earned 的分母); 0 = 全部已揭示, earned 无定义.")

    @property
    def rubric_coverage(self) -> float:
        if self.total_items == 0:
            return 0.0
        return self.passed_items / self.total_items

    @property
    def earned_coverage(self) -> float | None:
        """在 hint 尚未揭示的 item 里自己挣到的比例 (难度曲线的「真」信号)。"""
        if self.earned_total_items == 0:
            return None
        return self.earned_passed_items / self.earned_total_items
