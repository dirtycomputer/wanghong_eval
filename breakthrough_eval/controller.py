"""CONTROLLER — the scale-up engine / orchestrator (plan §6).

Expands one task into the `(task × eligible_model × hint_level × trial)`
cartesian product, enforces the contamination red-lines *before* scheduling,
runs each cell through PROVER → EVAL, persists artifacts, and supports:

  * eligibility filtering (`cutoff < retrieval_cutoff`, plan §6.2),
  * contamination early-stop (a contaminated (model,task) is removed and its
    remaining jobs are skipped, plan §8.3 + §10.5 budget),
  * the differential sanity check (frozen vs post-breakthrough arXiv, plan §8).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from .arxiv_frozen import ArxivFrozenSource, InMemoryArxivSource
from .eval.evaluator import Evaluator
from .models import EvalResult, Job, ModelEntry, ProverRunResult, TaskSpec
from .prover.base import ProverBackend, get_backend
from .prover.runner import ProverRunner
from .registry import ModelRegistry
from .storage import ResultStore

log = logging.getLogger(__name__)

SourceFactory = Callable[[date], ArxivFrozenSource]


@dataclass
class BackendSpec:
    provider: str = "mock"
    kwargs: dict = field(default_factory=dict)


@dataclass
class JobMatrix:
    jobs: list[Job]
    skipped: list[tuple[str, str, str]]  # (task_id, model, reason)


def default_source_factory(cutoff: date) -> ArxivFrozenSource:
    return InMemoryArxivSource(cutoff)


class Controller:
    def __init__(
        self,
        tasks: dict[str, TaskSpec],
        registry: ModelRegistry,
        evaluator: Evaluator,
        store: Optional[ResultStore] = None,
        source_factory: SourceFactory = default_source_factory,
        backend_specs: Optional[dict[str, BackendSpec]] = None,
    ):
        self.tasks = tasks
        self.registry = registry
        self.evaluator = evaluator
        self.store = store
        self.source_factory = source_factory
        self.backend_specs = backend_specs or {}
        self._backend_cache: dict[str, ProverBackend] = {}
        # Probe results cached per (task_id, model): contamination is per
        # (model, task), so we probe once and reuse across hint levels/trials.
        self._probe_cache: dict[tuple[str, str], list] = {}

    # ------------------------------------------------------------------ #
    def backend_for(self, model: str) -> ProverBackend:
        if model not in self._backend_cache:
            entry: ModelEntry | None = self.registry.entries.get(model)
            spec = self.backend_specs.get(model)
            if spec is None:
                provider = entry.provider if entry else "mock"
                kwargs = dict(entry.backend_kwargs) if entry else {}
                spec = BackendSpec(provider=provider, kwargs=kwargs)
            self._backend_cache[model] = get_backend(spec.provider, **spec.kwargs)
        return self._backend_cache[model]

    # ---- job matrix (plan §6.4) --------------------------------------- #
    def expand_job_matrix(
        self,
        task_ids: list[str],
        model_names: Optional[list[str]] = None,
        hint_levels: Optional[list[int]] = None,
        trials: int = 1,
    ) -> JobMatrix:
        jobs: list[Job] = []
        skipped: list[tuple[str, str, str]] = []
        for task_id in task_ids:
            task = self.tasks[task_id]
            # Candidate models.
            if model_names is None:
                candidates = [e.name for e in self.registry.eligible_models(task)]
            else:
                candidates = list(model_names)

            levels = hint_levels or [h.level for h in task.hint_ladder]
            for model in candidates:
                entry = self.registry.entries.get(model)
                # Red-line eligibility check (plan §2, §6.2).
                if entry is not None and not entry.eligible_for(task):
                    skipped.append(
                        (task_id, model,
                         f"cutoff {entry.cutoff_date} >= retrieval_cutoff "
                         f"{task.retrieval_cutoff} (不合格)")
                    )
                    continue
                if entry is None:
                    skipped.append((task_id, model, "不在 model registry 中 (cutoff 未知)"))
                    continue
                for level in levels:
                    for trial in range(trials):
                        jobs.append(Job(task_id=task_id, model=model,
                                        hint_level=level, trial=trial))
        return JobMatrix(jobs=jobs, skipped=skipped)

    # ---- run loop ----------------------------------------------------- #
    def run_job(self, job: Job) -> tuple[ProverRunResult, EvalResult]:
        task = self.tasks[job.task_id]
        source = self.source_factory(task.retrieval_cutoff)
        runner = ProverRunner(self.backend_for(job.model), source)

        # Probe once per (model, task); reuse across hint levels/trials (plan §8.3).
        key = (job.task_id, job.model)
        cached_probes = self._probe_cache.get(key)
        log.info("▶ job %s 开始", job.job_id)
        t0 = time.perf_counter()
        prover_result = runner.run(task, job, probe_responses=cached_probes)
        if cached_probes is None and len(prover_result.probe_responses) == len(
            task.contamination_probes
        ):
            # Only cache a *complete* battery: a probe-phase infra error leaves a
            # partial one, and caching it would let later jobs skip real probing.
            self._probe_cache[key] = prover_result.probe_responses

        eval_result = self.evaluator.evaluate(prover_result, task)
        log.info(
            "✔ job %s 完成 %.1fs (contaminated=%s excluded=%s)",
            job.job_id, time.perf_counter() - t0,
            prover_result.contaminated, prover_result.excluded,
        )
        if self.store is not None:
            self.store.save_prover(prover_result)
            self.store.save_eval(eval_result)
        return prover_result, eval_result

    def run(
        self,
        matrix: JobMatrix,
        early_stop_on_contamination: bool = True,
        max_workers: int = 1,
    ) -> list[tuple[ProverRunResult, EvalResult]]:
        """Run every job, parallelising the expensive prove+eval phase.

        Two phases keep probe-caching and contamination early-stop correct under
        concurrency:
          1. **Prime (sequential):** for each (task, model) run its first job —
             this populates the probe cache and detects contamination. A
             contaminated (model, task) is removed and its remaining jobs are
             skipped (plan §8.3).
          2. **Fan-out:** the remaining clean jobs reuse the cached probes and
             run concurrently in a thread pool (work is HTTP-bound, so threads
             scale). ``max_workers=1`` keeps it sequential/deterministic.
        """
        results: list[tuple[ProverRunResult, EvalResult]] = []
        groups: dict[tuple[str, str], list[Job]] = defaultdict(list)
        for job in matrix.jobs:
            groups[(job.task_id, job.model)].append(job)

        log.info(
            "运行: %d job, %d 个 (task,model) 组, max_workers=%d",
            len(matrix.jobs), len(groups), max_workers,
        )
        deferred: list[Job] = []
        # Phase 1 — prime each (task, model) sequentially.
        log.info("Phase 1 (prime): 每组首个 job 顺序跑 (探针 + 污染判定)")
        for (task_id, model), jobs in groups.items():
            jobs.sort(key=lambda j: (j.hint_level, j.trial))
            first = jobs[0]
            pr, ev = self.run_job(first)  # cache miss → runs probes here
            results.append((pr, ev))
            if early_stop_on_contamination and pr.contaminated:
                log.warning(
                    "(%s,%s) 污染除名, 早停跳过其余 %d 个 job", task_id, model, len(jobs) - 1
                )
                for j in jobs[1:]:
                    matrix.skipped.append(
                        (j.task_id, j.model, f"早停: 已判污染除名, 跳过 {j.job_id}")
                    )
                continue
            deferred.extend(jobs[1:])

        # Phase 2 — fan out the remaining clean jobs (probes cached).
        if deferred:
            log.info("Phase 2 (fan-out): 并发跑 %d 个 job, workers=%d", len(deferred), max_workers)
            if max_workers and max_workers > 1:
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    results.extend(ex.map(self.run_job, deferred))
            else:
                results.extend(self.run_job(job) for job in deferred)
        log.info("运行结束: %d 个 result", len(results))
        return results

    # ---- differential sanity check (plan §8) -------------------------- #
    def differential_sanity_check(
        self, task_id: str, model: str, hint_level: int = 0, trials: int = 3
    ) -> dict:
        """Run frozen vs post-breakthrough arXiv; the gap validates the benchmark.

        If giving post-T literature suddenly lets the model solve while the
        frozen version can't, the metric is measuring independence (good), and
        the frozen run is confirmed un-contaminated.
        """
        task = self.tasks[task_id]
        backend = self.backend_for(model)

        def _solve_rate(cutoff: date) -> float:
            # Measure *raw* solve capability: score the proof text directly,
            # deliberately bypassing the audit/exclusion guard (the point is to
            # see whether feeding post-T literature lets it solve at all).
            source = self.source_factory(cutoff)
            runner = ProverRunner(backend, source)
            valids = 0
            for trial in range(trials):
                job = Job(task_id=task_id, model=model, hint_level=hint_level, trial=trial)
                pr = runner.run(task, job)
                if pr.contaminated or pr.structured_output is None:
                    continue
                ev = self.evaluator.evaluate_text(pr.job_id, task, pr.proof_text)
                valids += int(ev.overall_valid)
            return valids / trials if trials else 0.0

        frozen = _solve_rate(task.retrieval_cutoff)
        post = _solve_rate(task.breakthrough_date)  # deliberately leaky
        return {
            "task_id": task_id,
            "model": model,
            "hint_level": hint_level,
            "frozen_solve_rate": frozen,
            "post_breakthrough_solve_rate": post,
            "gap": post - frozen,
        }
