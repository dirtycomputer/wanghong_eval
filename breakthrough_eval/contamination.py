"""Contamination control logic (plan §8) — the make-or-break layer.

Two of the four defence lines live here as reusable functions:

  * probe battery evaluation (defence line 3): does an *unhinted* response
    already spit out the breakthrough's key innovation?
  * post-hoc audit (defence line 4): are all cited references <= cutoff, and was
    there any out-of-window network call in the transcript?

(Line 1 = model cutoff, enforced by ``ModelEntry.eligible_for`` /
``Controller``; line 2 = frozen retrieval, enforced inside ``ArxivFrozenSource``.)
"""

from __future__ import annotations

import json
import re
from datetime import date

from .arxiv_frozen import ArxivFrozenSource
from .models import (
    AuditResult,
    ContaminationProbe,
    ProbeResponse,
    ToolCall,
)


def _match_indicators(text: str, indicators: list[str]) -> list[str]:
    """Case-insensitive, word-ish containment match. Returns the matched ones."""
    low = text.lower()
    hits: list[str] = []
    for ind in indicators:
        if ind.lower() in low:
            hits.append(ind)
    return hits


def evaluate_probe(probe: ContaminationProbe, response_text: str) -> ProbeResponse:
    """Decide whether a single probe response leaks the key innovation (plan §8.3)."""
    matched = _match_indicators(response_text, probe.leak_indicators)
    leaked = len(matched) >= probe.threshold
    return ProbeResponse(
        probe_id=probe.id,
        kind=probe.kind,
        response_text=response_text,
        matched_indicators=matched,
        leaked=leaked,
    )


def any_leaked(probe_responses: list[ProbeResponse]) -> bool:
    """任一探针命中关键创新 → 判污染 (plan §8.3)."""
    return any(p.leaked for p in probe_responses)


# --------------------------------------------------------------------------- #
# Semantic probe judging (plan §8.3 升级): 关键词是下限, 这里抓换措辞的实质复现
# --------------------------------------------------------------------------- #
PROBE_JUDGE_SYSTEM = """\
你是污染审计员。被审计的模型其训练截止在该数学突破公布**之前**, 正常情况下它不应
知道这个突破。请判断它对探针问题的回答是否「泄露」—— 即实质性复现/指认了突破的
关键创新, **即便措辞与参考关键词完全不同**。

判定要点:
- 算泄露: 表现出对突破内容的事实性知识 —— 声称该问题已被解决并给出与真实证明
  一致的特有结构/路线; 指认真实作者、时间或论文; 对参考关键创新的同义转述。
- 不算泄露: 诚实表示不知道/未解决; 给出泛泛的、领域常识级的猜想或思路
  (独立的数学直觉不是污染); 与真实证明无关的错误尝试。
- 探针是除名级裁决: 拿不准时判不泄露 (关键词命中另有一票否决兜底)。

只输出一个 JSON 对象, 不要 markdown 代码块:
{"leaked": true/false, "evidence": "不超过 60 字的判定依据", "confidence": 0.0-1.0}
"""


def build_probe_judge_prompt(task, probe: ContaminationProbe, response_text: str) -> str:
    deltas = "\n".join(
        f"- {r.id}: {r.title} — {r.criterion}" for r in task.rubric if r.frontier_delta
    )
    return (
        f"# 突破\n{task.title} (golden: {task.golden_proof.primary})\n\n"
        f"# 突破的关键创新 (判定参照)\n{deltas}\n"
        f"参考泄露措辞 (同义转述也算): {', '.join(probe.leak_indicators)}\n\n"
        f"# 探针问题 ({probe.kind.value})\n{probe.prompt}\n\n"
        f"# 被审计模型的回答\n{response_text}\n"
    )


class SemanticProbeJudge:
    """LLM 语义级泄露评审。注入 ``complete(system, user) -> str`` (返回 JSON)。

    解析失败时弃权 (返回不泄露 + 留痕): 探针的保守方向是不冤杀,
    关键词一票否决仍然兜底。
    """

    def __init__(self, complete, name: str = "semantic-probe", max_parse_retries: int = 1):
        self._complete = complete
        self.name = name
        self.max_parse_retries = max_parse_retries

    def describe(self) -> dict:
        info = {"name": self.name, "kind": "SemanticProbeJudge"}
        client = getattr(self._complete, "client", None)
        if client is not None:
            info.update(model=client.model, max_tokens=client.max_tokens,
                        temperature=client.temperature)
        return info

    def assess(self, task, probe: ContaminationProbe, response_text: str) -> tuple[bool, str]:
        user = build_probe_judge_prompt(task, probe, response_text)
        last_err = ""
        for _ in range(self.max_parse_retries + 1):
            try:
                raw = self._complete(PROBE_JUDGE_SYSTEM, user)
                start, end = raw.find("{"), raw.rfind("}")
                if start == -1 or end == -1:
                    raise ValueError(f"找不到 JSON: {raw[:120]!r}")
                data = json.loads(raw[start:end + 1])
                leaked = bool(data.get("leaked", False))
                notes = (
                    f"{'命中' if leaked else 'clean'} "
                    f"(conf={data.get('confidence', '?')}): {data.get('evidence', '')}"
                )
                return leaked, notes
            except Exception as exc:  # noqa: BLE001 - 弃权而非中断
                last_err = f"{type(exc).__name__}: {exc}"
        return False, f"语义评审弃权 (输出无法解析): {last_err[:160]}"


# --------------------------------------------------------------------------- #
# Post-hoc audit (plan §8.4)
# --------------------------------------------------------------------------- #
_ARXIV_RE = re.compile(r"arxiv[:\s]*([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})", re.I)


def _extract_arxiv_ids(reference: str) -> list[str]:
    return [m.group(1) for m in _ARXIV_RE.finditer(reference)]


def audit_references(
    cited_references: list[str],
    source: ArxivFrozenSource,
    cutoff: date,
) -> list[str]:
    """Return references that resolve to a paper submitted after the cutoff.

    A reference is flagged if it names an arXiv id whose true submission date is
    > cutoff (i.e. the model somehow cited post-T material). References that do
    not resolve in the corpus are left alone (can't prove they're out of window).
    """
    out_of_window: list[str] = []
    for ref in cited_references:
        for arxiv_id in _extract_arxiv_ids(ref):
            paper = _resolve_unfiltered(source, arxiv_id)
            if paper is not None and paper.submission_date > cutoff:
                out_of_window.append(ref)
                break
    return out_of_window


def _resolve_unfiltered(source: ArxivFrozenSource, arxiv_id: str):
    """Look a paper up ignoring the cutoff filter (auditors may see everything)."""
    try:
        for p in source._corpus():  # noqa: SLF001 - auditor needs full view
            if p.arxiv_id == arxiv_id:
                return p
    except NotImplementedError:
        return None
    return None


def audit_transcript(
    transcript: list[ToolCall],
    allowed_hosts: set[str],
    cutoff: date,
) -> list[str]:
    """Return descriptions of any out-of-window network access (plan §3.2, §8.4)."""
    violations: list[str] = []
    for call in transcript:
        if call.host and call.host not in allowed_hosts:
            violations.append(f"{call.tool} -> {call.host} (host not whitelisted)")
        if any(d > cutoff for d in call.returned_dates):
            violations.append(
                f"{call.tool} returned post-cutoff material for query '{call.query}'"
            )
    return violations


def audit_run(
    cited_references: list[str],
    transcript: list[ToolCall],
    source: ArxivFrozenSource,
    cutoff: date,
    allowed_hosts: set[str],
) -> AuditResult:
    return AuditResult(
        out_of_window_references=audit_references(cited_references, source, cutoff),
        out_of_window_network=audit_transcript(transcript, allowed_hosts, cutoff),
    )
