"""Leaderboard aggregation for void infra errors and excluded runs."""

from breakthrough_eval.leaderboard import Leaderboard
from breakthrough_eval.specification import EvalResult, ProverRunResult


def _pair(level, trial=0, valid=False, error=None, contaminated=False):
    jid = f"t::m::L{level}::t{trial}"
    pr = ProverRunResult(
        job_id=jid,
        task_id="t",
        model="m",
        hint_level=level,
        trial=trial,
        harness="m+direct-v",
        contaminated=contaminated,
        error=error,
    )
    ev = EvalResult(
        job_id=jid,
        task_id="t",
        overall_valid=valid,
        passed_items=int(valid),
        total_items=1,
        excluded=pr.excluded,
        errored=error is not None,
    )
    return pr, ev


def test_leaderboard_drops_errored_runs_from_solve_rate():
    # 网络抖动不是模型没解出来: errored trial 不进 solve_rate 分母。
    p0, e0 = _pair(0, trial=0, valid=True)
    p1, e1 = _pair(0, trial=1, error="LLMError: timeout")
    board = Leaderboard.build([p0, p1], [e0, e1], max_hint_level=1)
    row = board.rows[0]
    assert row.points[0].solve_rate == 1.0
    assert row.points[0].n_errors == 1
    assert row.error_count == 1
    assert row.probe_cleanliness == 1.0 and not row.removed
    assert "错误" in board.render_main_table()


def test_level_with_only_errored_trials_feeds_no_auc_point():
    # 某 hint 级全部 errored: 没有证据, 不向 AUC 喂假 0 点。
    p0, e0 = _pair(0, valid=True)
    p1, e1 = _pair(1, error="LLMError: timeout")
    board = Leaderboard.build([p0, p1], [e0, e1], max_hint_level=1)
    row = board.rows[0]
    assert row.hint_auc == 1.0
    assert [p.n_errors for p in row.points] == [0, 1]


def test_all_completed_excluded_runs_are_removed():
    p0, e0 = _pair(0, contaminated=True)
    p1, e1 = _pair(1, contaminated=True)
    board = Leaderboard.build([p0, p1], [e0, e1], max_hint_level=1)
    row = board.rows[0]
    assert row.removed
    assert row.probe_cleanliness == 0.0
