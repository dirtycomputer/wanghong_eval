"""CONTROLLER — the scale-up engine / orchestrator (plan §6).

Expands one task into the `(task × eligible_model × hint_level × trial)`
cartesian product, enforces the contamination red-lines *before* scheduling,
runs each cell through PROVER → EVAL, persists artifacts, and supports:

  * eligibility filtering (`cutoff < retrieval_cutoff`, plan §6.2),
  * contamination early-stop (a contaminated (model,task) is removed and its
    remaining jobs are skipped, plan §8.3 + §10.5 budget).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from .eval.evaluator import Evaluator
from .specification import EvalResult, Job, ModelEntry, ProverRunResult, TaskSpec
from .prover.backends import make_backend
from .prover.base import ProverBackend
from .prover.runner import ProverRunner
from .specification import ModelRegistry
from .storage import ResultStore

log = logging.getLogger(__name__)


@dataclass
class JobMatrix:
    jobs: list[Job]
    skipped: list[tuple[str, str, str]]  # (task_id, model, reason)


class Controller:
    def __init__(
        self,
        tasks: dict[str, TaskSpec],
        registry: ModelRegistry,
        evaluator: Evaluator,
        store: Optional[ResultStore] = None,
        probe_judge=None,
    ):
        self.tasks = tasks
        self.registry = registry
        self.evaluator = evaluator
        self.store = store
        self.probe_judge = probe_judge  # 语义级探针泄露评审 (可选)
        self._backend_cache: dict[str, ProverBackend] = {}
        # Probe results cached per (task_id, 底层模型权重): contamination is a
        # property of the *weights*, not of the harness/registry alias — 同一
        # 模型挂三个 harness 只探一次, 也保证同权重判定一致 (plan §8.3)。
        self._probe_cache: dict[tuple[str, str], list] = {}

    def _probe_key(self, job: Job) -> tuple[str, str]:
        """探针缓存键: 优先 api.model (真实权重 id), 否则 registry 名。"""
        entry = self.registry.entries.get(job.model)
        ident = job.model
        if entry is not None and entry.api is not None:
            ident = entry.api.model
        return (job.task_id, ident)

    # ------------------------------------------------------------------ #
    def backend_for(self, model: str) -> ProverBackend:
        if model not in self._backend_cache:
            entry: ModelEntry | None = self.registry.entries.get(model)
            if entry is None:
                raise KeyError(f"model registry 里没有 '{model}'")
            self._backend_cache[model] = make_backend(entry.api, entry.harness)
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
        runner = ProverRunner(self.backend_for(job.model), probe_judge=self.probe_judge)

        # Probe once per (task, 权重); reuse across harnesses/hints/trials (plan §8.3).
        key = self._probe_key(job)
        cached_probes = self._probe_cache.get(key)
        log.info("▶ job %s 开始", job.job_id)
        t0 = time.perf_counter()
        prover_result = runner.run(
            task,
            job,
            probe_responses=cached_probes,
            log_dir=(self.store.prover_dir / "log") if self.store is not None else None,
        )
        if cached_probes is None and len(prover_result.probe_responses) == len(
            task.contamination_probes
        ):
            # Only cache a *complete* battery: a probe-phase infra error leaves a
            # partial one, and caching it would let later jobs skip real probing.
            self._probe_cache[key] = prover_result.probe_responses

        if self.store is not None:
            self.store.save_prover(prover_result)

        eval_result = self.evaluator.evaluate(prover_result, task)
        log.info(
            "✔ job %s 完成 %.1fs (contaminated=%s excluded=%s)",
            job.job_id, time.perf_counter() - t0,
            prover_result.contaminated, prover_result.excluded,
        )
        if self.store is not None:
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
