"""Controller orchestration + leaderboard aggregation (plan §6, §7) + e2e."""

from breakthrough_eval.controller import Controller
from breakthrough_eval.eval.evaluator import Evaluator
from breakthrough_eval.eval.mock import MockJudge, MockJudgeConfig
from breakthrough_eval.leaderboard import Leaderboard


def _evaluator():
    return Evaluator(
        [
            MockJudge(MockJudgeConfig(strictness=0.4, name="lenient")),
            MockJudge(MockJudgeConfig(strictness=0.5, name="balanced")),
            MockJudge(MockJudgeConfig(strictness=0.7, name="strict")),
        ]
    )


def _controller(tasks, registry):
    return Controller(tasks=tasks, registry=registry, evaluator=_evaluator())


def test_job_matrix_filters_ineligible_models(tasks, registry, task):
    ctrl = _controller(tasks, registry)
    matrix = ctrl.expand_job_matrix([task.task_id], trials=1)
    models_in_jobs = {j.model for j in matrix.jobs}
    # post-cutoff-frontier has cutoff after retrieval_cutoff → auto-excluded
    assert "post-cutoff-frontier" not in models_in_jobs
    # the eligible ones are present
    assert "open-precutoff-strong" in models_in_jobs


def test_explicit_ineligible_model_skipped(tasks, registry, task):
    ctrl = _controller(tasks, registry)
    matrix = ctrl.expand_job_matrix(
        [task.task_id], model_names=["post-cutoff-frontier"], trials=1
    )
    assert matrix.jobs == []
    assert matrix.skipped


def test_early_stop_on_contamination(tasks, registry, task):
    ctrl = _controller(tasks, registry)
    matrix = ctrl.expand_job_matrix(
        [task.task_id], model_names=["leaky-eligible-by-date"], trials=3
    )
    n_jobs = len(matrix.jobs)
    results = ctrl.run(matrix, early_stop_on_contamination=True)
    # Only the first job runs (contaminated); the rest are skipped.
    assert len(results) == 1
    assert results[0][0].contaminated
    assert len(matrix.skipped) == n_jobs - 1


def test_full_pipeline_and_leaderboard(tasks, registry, task):
    ctrl = _controller(tasks, registry)
    matrix = ctrl.expand_job_matrix(
        [task.task_id],
        model_names=["open-precutoff-strong", "open-precutoff-weak", "leaky-eligible-by-date"],
        trials=4,
    )
    results = ctrl.run(matrix)
    prover = [pr for pr, _ in results]
    evals = [ev for _, ev in results]
    board = Leaderboard.build(prover, evals, max_hint_level=task.max_hint_level)

    by_harness = {r.harness: r for r in board.rows}
    # contaminated model is removed
    leaky = next(r for r in board.rows if "leaky" in r.harness)
    assert leaky.removed

    # cold start (L0) yields no solves for anyone (Einstein-test difficulty)
    for r in board.rows:
        if not r.removed:
            assert r.l0_pass_at_k is False

    # stronger model has >= hint-AUC than the weak one
    strong = next(r for r in board.rows if "strong" in r.harness)
    weak = next(r for r in board.rows if "weak" in r.harness)
    assert strong.hint_auc >= weak.hint_auc
    assert strong.peak_rubric_coverage > 0

    # rendering doesn't crash
    assert "hint-AUC" in board.render_main_table()
    assert "难度曲线" in board.render_difficulty_curve(strong.harness, task.task_id)


def test_probes_cached_per_model_task(tasks, registry, task):
    # Probes must run once per (model, task), not per hint level (plan §8.3).
    from breakthrough_eval.prover.base import ProverContext
    from breakthrough_eval.prover.mock import MockProverBackend, MockProverConfig

    backend = MockProverBackend(MockProverConfig(capability=0.6))
    probe_calls = {"n": 0}
    orig = backend.run

    def counting(ctx: ProverContext):
        if ctx.phase == "probe":
            probe_calls["n"] += 1
        return orig(ctx)

    backend.run = counting

    ctrl = _controller(tasks, registry)
    ctrl.backend_specs = {}
    ctrl._backend_cache["open-precutoff-strong"] = backend  # inject counting backend

    matrix = ctrl.expand_job_matrix(
        [task.task_id], model_names=["open-precutoff-strong"], hint_levels=[0, 1, 2, 3], trials=2
    )
    ctrl.run(matrix)
    # 3 probes x exactly one (model, task), regardless of 4 hints x 2 trials = 8 jobs.
    assert probe_calls["n"] == len(task.contamination_probes)


def test_partial_probe_battery_not_cached(tasks, registry, task):
    # 探针阶段首跑出错 → 不缓存半截电池; 后续 job 重跑完整探针并正常证明。
    from breakthrough_eval.prover.mock import MockProverBackend, MockProverConfig

    backend = MockProverBackend(MockProverConfig(capability=0.6))
    state = {"probe_calls": 0, "failed_once": False}
    orig = backend.run

    def flaky(ctx):
        if ctx.phase == "probe":
            state["probe_calls"] += 1
            if not state["failed_once"]:
                state["failed_once"] = True
                raise RuntimeError("transient network error")
        return orig(ctx)

    backend.run = flaky
    ctrl = _controller(tasks, registry)
    ctrl._backend_cache["open-precutoff-strong"] = backend
    matrix = ctrl.expand_job_matrix(
        [task.task_id], model_names=["open-precutoff-strong"], hint_levels=[0], trials=2
    )
    results = ctrl.run(matrix)
    assert len(results) == 2
    first, second = results[0][0], results[1][0]
    assert first.error is not None and "transient" in first.error
    assert second.error is None and second.structured_output is not None
    # 首个 job 只发出 1 次 (失败的) 探针调用; 第二个 job 重跑完整电池而非复用半截缓存。
    assert state["probe_calls"] == 1 + len(task.contamination_probes)
    # 错误 run 的 eval 是作废 (errored), 不是 0 分的「模型失败」也不是污染除名。
    assert results[0][1].errored and not results[0][1].excluded


def test_leaderboard_drops_errored_runs_from_solve_rate():
    # 网络抖动 ≠ 模型没解出来: errored trial 不进 solve_rate 分母, 单列计数。
    from breakthrough_eval.models import EvalResult, ProverRunResult

    def _pair(level, trial, valid=False, error=None):
        jid = f"t::m::L{level}::t{trial}"
        pr = ProverRunResult(
            job_id=jid, task_id="t", model="m", hint_level=level, trial=trial,
            harness="m+mock-v", error=error,
        )
        ev = EvalResult(
            job_id=jid, task_id="t", overall_valid=valid,
            passed_items=int(valid), total_items=1, errored=error is not None,
        )
        return pr, ev

    p0, e0 = _pair(0, 0, valid=True)
    p1, e1 = _pair(0, 1, error="LLMError: timeout")
    board = Leaderboard.build([p0, p1], [e0, e1], max_hint_level=1)
    row = board.rows[0]
    assert row.points[0].solve_rate == 1.0  # 1/1 clean, 而不是 1/2
    assert row.points[0].n_errors == 1
    assert row.error_count == 1
    assert row.probe_cleanliness == 1.0 and not row.removed
    assert "错误" in board.render_main_table()


def test_level_with_only_errored_trials_feeds_no_auc_point():
    # 某 hint 级全部 errored → 无证据, 不向 AUC 喂假 0 点。
    from breakthrough_eval.models import EvalResult, ProverRunResult

    def _pair(level, valid=False, error=None):
        jid = f"t::m::L{level}::t0"
        pr = ProverRunResult(
            job_id=jid, task_id="t", model="m", hint_level=level, trial=0,
            harness="m+mock-v", error=error,
        )
        ev = EvalResult(
            job_id=jid, task_id="t", overall_valid=valid,
            passed_items=int(valid), total_items=1, errored=error is not None,
        )
        return pr, ev

    p0, e0 = _pair(0, valid=True)
    p1, e1 = _pair(1, error="LLMError: timeout")
    board = Leaderboard.build([p0, p1], [e0, e1], max_hint_level=1)
    row = board.rows[0]
    assert row.hint_auc == 1.0  # 只有 L0 的 1.0; 不被 L1 的假 0 拉成 0.5
    assert [p.n_errors for p in row.points] == [0, 1]


def test_parallel_matches_sequential(tasks, registry, task):
    # Parallel execution must produce the same aggregated leaderboard as serial
    # (mock backend is deterministic per job_id).
    models = ["open-precutoff-strong", "open-precutoff-weak"]

    def _board(workers):
        ctrl = _controller(tasks, registry)
        matrix = ctrl.expand_job_matrix([task.task_id], model_names=models, trials=2)
        results = ctrl.run(matrix, max_workers=workers)
        prover = [pr for pr, _ in results]
        evals = [ev for _, ev in results]
        return Leaderboard.build(prover, evals, max_hint_level=task.max_hint_level)

    serial = _board(1)
    parallel = _board(6)
    s = {r.harness: (round(r.hint_auc, 6), r.peak_rubric_coverage) for r in serial.rows}
    p = {r.harness: (round(r.hint_auc, 6), r.peak_rubric_coverage) for r in parallel.rows}
    assert s == p
    # and every job ran exactly once
    assert sum(len(r.points) for r in parallel.rows) == sum(len(r.points) for r in serial.rows)


def test_differential_sanity_check_shows_gap(tasks, registry, task):
    # Frozen arXiv hides the golden paper → cold-start solve rate 0.
    # Post-breakthrough arXiv exposes it → the model can copy the answer.
    # The positive gap confirms the metric measures independence (plan §8).
    ctrl = _controller(tasks, registry)
    report = ctrl.differential_sanity_check(
        task.task_id, "open-precutoff-weak", hint_level=0, trials=3
    )
    assert set(report) >= {"frozen_solve_rate", "post_breakthrough_solve_rate", "gap"}
    assert report["frozen_solve_rate"] == 0.0
    assert report["post_breakthrough_solve_rate"] > report["frozen_solve_rate"]
