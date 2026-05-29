"""Multi-judge evaluator: consensus, agreement, golden anchoring (plan §4.2).

  * ≥1 judges score independently; per-item consensus is majority vote.
  * agreement = mean pairwise Cohen's κ over the per-item binary labels.
  * needs_human_review fires on low κ, disagreement on overall validity, or
    disagreement on any *frontier-delta* item.
  * golden anchoring: feed the golden proof through the same pipeline; it must
    score full marks, otherwise the rubric/judges are miscalibrated.
"""

from __future__ import annotations

from itertools import combinations

from ..models import EvalResult, GoldenProof, JudgeVerdict, ProverRunResult, TaskSpec
from .base import JudgeBackend


def cohen_kappa(a: list[bool], b: list[bool]) -> float:
    """Cohen's κ for two binary raters over paired observations."""
    n = len(a)
    if n == 0:
        return 1.0
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa_true = sum(a) / n
    pb_true = sum(b) / n
    pe = pa_true * pb_true + (1 - pa_true) * (1 - pb_true)
    if pe >= 1.0:  # both raters constant & identical class distribution
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def mean_pairwise_kappa(verdicts: list[JudgeVerdict], item_ids: list[str]) -> float:
    if len(verdicts) < 2:
        return 1.0
    label_vectors = [[v.verdict_map().get(i, False) for i in item_ids] for v in verdicts]
    kappas = [cohen_kappa(x, y) for x, y in combinations(label_vectors, 2)]
    return sum(kappas) / len(kappas) if kappas else 1.0


class Evaluator:
    def __init__(self, judges: list[JudgeBackend], review_kappa_threshold: float = 0.6):
        if not judges:
            raise ValueError("Evaluator 至少需要一个 judge。")
        self.judges = judges
        self.review_kappa_threshold = review_kappa_threshold

    # ------------------------------------------------------------------ #
    def evaluate(self, result: ProverRunResult, task: TaskSpec) -> EvalResult:
        if result.excluded:
            # Contaminated / audit-failed runs are scored as excluded (plan §7, §8).
            return EvalResult(
                job_id=result.job_id,
                task_id=task.task_id,
                total_items=len(task.rubric),
                excluded=True,
                needs_human_review=False,
            )
        return self.evaluate_text(result.job_id, task, result.proof_text)

    def evaluate_text(self, job_id: str, task: TaskSpec, proof_text: str) -> EvalResult:
        item_ids = [r.id for r in task.rubric]
        delta_ids = {r.id for r in task.rubric if r.frontier_delta}

        all_verdicts = [j.judge(task, proof_text, task.golden_proof) for j in self.judges]
        # Judges whose output couldn't be parsed abstain (don't vote), but force
        # the cell into the human-review queue (plan §4.2).
        verdicts = [v for v in all_verdicts if not v.parse_failed]
        had_parse_failure = len(verdicts) < len(all_verdicts)

        # Per-item majority consensus (over voting judges only).
        consensus: dict[str, bool] = {}
        item_disagreement: set[str] = set()
        for iid in item_ids:
            votes = [v.verdict_map().get(iid, False) for v in verdicts]
            consensus[iid] = bool(votes) and sum(votes) * 2 > len(votes)  # strict majority
            if any(votes) and not all(votes):
                item_disagreement.add(iid)

        passed_items = sum(1 for ok in consensus.values() if ok)
        overall_votes = [v.overall_valid for v in verdicts]
        overall_valid = bool(overall_votes) and sum(overall_votes) * 2 > len(overall_votes)
        alt_votes = [v.alternative_valid for v in verdicts]
        alternative_valid = bool(alt_votes) and sum(alt_votes) * 2 > len(alt_votes)

        agreement = mean_pairwise_kappa(verdicts, item_ids)
        needs_review = (
            had_parse_failure
            or not verdicts
            or agreement < self.review_kappa_threshold
            or (any(overall_votes) and not all(overall_votes))
            or bool(item_disagreement & delta_ids)
        )

        return EvalResult(
            job_id=job_id,
            task_id=task.task_id,
            judges=all_verdicts,  # keep abstaining/parse-failed verdicts for transparency
            item_consensus=consensus,
            passed_items=passed_items,
            total_items=len(item_ids),
            overall_valid=overall_valid,
            alternative_valid=alternative_valid,
            agreement=agreement,
            needs_human_review=needs_review,
            excluded=False,
        )

    # ---- golden anchoring / calibration (plan §4.2) ------------------- #
    def calibrate_with_golden(self, task: TaskSpec) -> EvalResult:
        """Score a synthetic 'perfect' proof; it must reach full coverage."""
        return self.evaluate_text(
            job_id=f"{task.task_id}::__golden__",
            task=task,
            proof_text=golden_reference_text(task),
        )


def golden_reference_text(task: TaskSpec) -> str:
    """Build a maximal proof text that exercises every rubric indicator.

    Used as the calibration anchor: the judge pipeline must give it full marks
    (plan §4.2). It is NOT given to PROVER.
    """
    lines = ["# Claimed Proof", "## 1. 思路总览", "完整覆盖所有关键创新点。"]
    lines.append("## 2. 关键引理与论证")
    for item in task.rubric:
        lines.append(f"{item.id} {item.title}: " + "; ".join(item.indicators))
    lines.append("## 3. 完整证明")
    lines.append("组合上述全部引理闭合归纳, 得到满维结论。")
    lines.append("## 4. 引用文献")
    lines.append(f"- {task.golden_proof.primary}")
    return "\n".join(lines)
