"""Leaderboard aggregation (plan §7).

One row per (harness, task), aggregated across hint levels and trials:

  * **hint-AUC** — area under ``solve_rate vs hint_level``; lower hint to solve
    ⇒ higher AUC ⇒ closer to true breakthrough ability (plan §5).
  * **L0 pass@k**, **lowest solvable hint**, **peak rubric coverage**.
  * **probe cleanliness** — contaminated / audit-failed runs are excluded from
    scoring and shown explicitly (plan §7, §8). A (harness, task) with no clean
    runs is *removed* from the ranking.
  * **errors** — infra-errored runs (network/backend failures) are *void*: they
    count neither as model failures (solve_rate/AUC 只看 clean trials) nor as
    contamination (不进洁净度分母), and surface in their own column.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .specification import EvalResult, ProverRunResult


@dataclass
class HintPoint:
    hint_level: int
    n_trials: int
    n_excluded: int
    n_errors: int  # infra-errored trials (void: not failures, not contamination)
    solve_rate: float  # fraction of clean trials judged overall_valid
    mean_coverage: float  # mean rubric coverage over clean trials
    pass_at_k: bool  # any clean trial valid
    # 在 hint 尚未揭示的 item 里挣到的覆盖率均值; None = 全部已揭示/无数据。
    # (给 hint 白送的项不算 —— coverage 的「真」难度信号, plan §5)
    mean_earned_coverage: float | None = None


@dataclass
class LeaderboardRow:
    harness: str
    task_id: str
    points: list[HintPoint] = field(default_factory=list)
    hint_auc: float = 0.0
    hint_auc_coverage: float = 0.0
    l0_pass_at_k: bool = False
    lowest_solvable_hint: int | None = None
    peak_rubric_coverage: float = 0.0
    peak_earned_coverage: float | None = None  # earned 的峰值; None = 无可挣项数据
    probe_cleanliness: float = 1.0  # 1 - excluded/completed (errored runs 不进分母)
    removed: bool = False  # 污染除名
    cost_units: float = 0.0
    needs_review_count: int = 0
    error_count: int = 0  # 基础设施错误的 run 数 (作废, 不计入 solve_rate)


def _auc(points: list[tuple[float, float]]) -> float:
    """Trapezoidal integral of y over x, normalised by the x-span."""
    pts = sorted(points)
    if not pts:
        return 0.0
    if len(pts) == 1:
        return pts[0][1]
    area = sum(
        (x1 - x0) * (y0 + y1) / 2.0
        for (x0, y0), (x1, y1) in zip(pts, pts[1:])
    )
    span = pts[-1][0] - pts[0][0]
    return area / span if span > 0 else pts[0][1]


class Leaderboard:
    def __init__(self, rows: list[LeaderboardRow]):
        self.rows = rows

    # ------------------------------------------------------------------ #
    @classmethod
    def build(
        cls,
        prover_results: list[ProverRunResult],
        eval_results: list[EvalResult],
        max_hint_level: int | None = None,
    ) -> "Leaderboard":
        evals = {e.job_id: e for e in eval_results}
        # group by (harness, task) -> hint_level -> list of (prover, eval)
        groups: dict[tuple[str, str], dict[int, list[tuple[ProverRunResult, EvalResult]]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        for pr in prover_results:
            ev = evals.get(pr.job_id)
            if ev is None:
                continue
            groups[(pr.harness, pr.task_id)][pr.hint_level].append((pr, ev))

        rows: list[LeaderboardRow] = []
        for (harness, task_id), by_level in groups.items():
            inferred_max = max_hint_level or max(by_level) or 1
            row = LeaderboardRow(harness=harness, task_id=task_id)

            total = excluded = errors = review = 0
            cost = 0.0
            solve_points: list[tuple[float, float]] = []
            cov_points: list[tuple[float, float]] = []

            for level in sorted(by_level):
                pairs = by_level[level]
                # Three buckets: excluded (污染/审计作废) > errored (基础设施错误,
                # 作废但非污染) > clean. Only clean trials carry scoring signal —
                # 网络抖动不算「模型没解出来」.
                n_excl = sum(1 for p, _ in pairs if p.excluded)
                n_err = sum(1 for p, _ in pairs if not p.excluded and p.error is not None)
                clean = [(p, e) for p, e in pairs if not p.excluded and p.error is None]
                total += len(pairs)
                excluded += n_excl
                errors += n_err
                cost += sum(p.usage.cost_units for p, _ in pairs)
                review += sum(1 for _, e in clean if e.needs_human_review)

                earned_vals = []
                if clean:
                    solve_rate = sum(1 for _, e in clean if e.overall_valid) / len(clean)
                    mean_cov = sum(e.rubric_coverage for _, e in clean) / len(clean)
                    pass_k = any(e.overall_valid for _, e in clean)
                    earned_vals = [e.earned_coverage for _, e in clean
                                   if e.earned_coverage is not None]
                    x = level / inferred_max if inferred_max else 0.0
                    solve_points.append((x, solve_rate))
                    cov_points.append((x, mean_cov))
                else:
                    # 该级没有任何 clean trial: 没有证据, 不向 AUC 喂假 0 点。
                    solve_rate = mean_cov = 0.0
                    pass_k = False
                mean_earned = (sum(earned_vals) / len(earned_vals)) if earned_vals else None

                row.points.append(
                    HintPoint(level, len(pairs), n_excl, n_err, solve_rate, mean_cov,
                              pass_k, mean_earned)
                )

                if level == 0:
                    row.l0_pass_at_k = pass_k
                if pass_k and row.lowest_solvable_hint is None:
                    row.lowest_solvable_hint = level
                row.peak_rubric_coverage = max(row.peak_rubric_coverage, mean_cov)
                if mean_earned is not None and (
                    row.peak_earned_coverage is None or mean_earned > row.peak_earned_coverage
                ):
                    row.peak_earned_coverage = mean_earned

            row.hint_auc = _auc(solve_points)
            row.hint_auc_coverage = _auc(cov_points)
            row.cost_units = cost
            row.needs_review_count = review
            row.error_count = errors
            # Cleanliness over *completed* runs only: an errored run says nothing
            # about contamination either way.
            completed = total - errors
            row.probe_cleanliness = 1.0 - (excluded / completed) if completed else 1.0
            row.removed = completed > 0 and excluded == completed
            rows.append(row)

        # Rank: clean first, then by hint-AUC (coverage as tiebreak).
        rows.sort(
            key=lambda r: (r.removed, -r.hint_auc, -r.hint_auc_coverage, -r.peak_rubric_coverage)
        )
        return cls(rows)

    # ------------------------------------------------------------------ #
    def difficulty_curve(self, harness: str, task_id: str) -> list[HintPoint]:
        for r in self.rows:
            if r.harness == harness and r.task_id == task_id:
                return r.points
        return []

    def render_main_table(self) -> str:
        header = (
            "| Rank | Harness | Task | hint-AUC ↑ | cov-AUC | L0 pass@k | "
            "最低可解 hint | rubric 峰值 | earned 峰值 | 探针洁净度 | 复核 | 错误 | 成本 |"
        )
        sep = "|" + "---|" * 13
        lines = [header, sep]
        for i, r in enumerate(self.rows, 1):
            if r.removed:
                lines.append(
                    f"| — | {r.harness} | {r.task_id} | — | — | — | — | — | — | "
                    f"❌ 污染除名 | — | {r.error_count} | {r.cost_units:.1f} |"
                )
                continue
            lowest = f"L{r.lowest_solvable_hint}" if r.lowest_solvable_hint is not None else "—"
            clean = "✅ clean" if r.probe_cleanliness >= 1.0 else f"⚠️ {r.probe_cleanliness:.0%}"
            earned = f"{r.peak_earned_coverage:.0%}" if r.peak_earned_coverage is not None else "—"
            lines.append(
                f"| {i} | {r.harness} | {r.task_id} | {r.hint_auc:.3f} | "
                f"{r.hint_auc_coverage:.3f} | {'✅' if r.l0_pass_at_k else '0%'} | "
                f"{lowest} | {r.peak_rubric_coverage:.0%} | {earned} | {clean} | "
                f"{r.needs_review_count} | {r.error_count} | {r.cost_units:.1f} |"
            )
        return "\n".join(lines)

    def render_difficulty_curve(self, harness: str, task_id: str) -> str:
        points = self.difficulty_curve(harness, task_id)
        if not points:
            return f"(无数据: {harness} / {task_id})"
        lines = [f"难度曲线 — {harness} @ {task_id}",
                 "hint  solve_rate  coverage  earned  bar"]
        for p in points:
            bar = "█" * int(round(p.solve_rate * 20))
            earned = (f"{p.mean_earned_coverage:>5.2f}"
                      if p.mean_earned_coverage is not None else "    —")
            lines.append(
                f"L{p.hint_level}    {p.solve_rate:>5.2f}      "
                f"{p.mean_coverage:>5.2f}   {earned}    {bar}"
            )
        return "\n".join(lines)
