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
