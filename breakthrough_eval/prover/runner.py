"""Probe-then-prove orchestration for a single Job (plan §3.3).

    1. Probe phase  — run every contamination probe. If any leaks the key
                      innovation, mark the run contaminated and STOP (no prove
                      phase, excluded from scoring).
    2. Prove phase  — build the hinted prompt, run the backend, parse the
                      structured writeup.
    3. Audit        — check every cited reference is <= cutoff and that no
                      out-of-window network call happened (plan §8.4).
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from breakthrough_eval.prover.tools.restricted_search import RESTRICTED_SEARCH_HOST
from .contamination import any_leaked, audit_run, evaluate_probe
from .prompts import PROBE_SYSTEM_PROMPT, PROVER_SYSTEM_PROMPT, probe_prompt, prove_prompt
from ..specification import (
    Job,
    ProverRunResult,
    ProverStructuredOutput,
    TaskSpec,
)
from .base import ProverBackend, ProverContext

log = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^##\s*(\d+)\.")


def parse_structured(text: str) -> ProverStructuredOutput:
    """Best-effort parse of the §3.4 structured format from raw text.

    Sections are delimited by ``## N.`` headings; the heading line itself (incl.
    its title) is dropped and only the body lines are kept.
    """
    sections: dict[int, str] = {}
    current: int | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = int(m.group(1))
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()

    refs = [
        line.lstrip("-* ").strip()
        for line in sections.get(4, "").splitlines()
        if line.strip().startswith(("-", "*"))
    ]
    return ProverStructuredOutput(
        idea_overview=sections.get(1, ""),
        key_lemmas=sections.get(2, ""),
        full_proof=sections.get(3, ""),
        cited_references=refs,
    )


class ProverRunner:
    def __init__(
        self,
        backend: ProverBackend,
        allowed_hosts: set[str] | None = None,
        probe_judge=None,
    ):
        self.backend = backend
        self.allowed_hosts = allowed_hosts or {RESTRICTED_SEARCH_HOST}
        # 语义级泄露评审 (contamination.SemanticProbeJudge): 关键词命中仍一票否决,
        # 这里抓换措辞的实质复现; None = 仅关键词 (离线默认)。
        self.probe_judge = probe_judge

    def _new_result(self, task: TaskSpec, job: Job) -> ProverRunResult:
        return ProverRunResult(
            job_id=job.job_id,
            task_id=task.task_id,
            model=job.model,
            hint_level=job.hint_level,
            trial=job.trial,
            harness=self.backend.harness_fingerprint(job.model),
        )

    def _probe_phase(
        self,
        task: TaskSpec,
        job: Job,
        result: ProverRunResult,
        log_dir: str | Path | None = None,
    ) -> None:
        """Run the probe battery into ``result``.

        A backend failure (network/provider) sets ``result.error`` and leaves the
        battery incomplete: the caller must neither treat the run as probed-clean
        nor cache the partial battery.
        """
        log.info("job %s: 探针阶段 (%d 个探针)", job.job_id, len(task.contamination_probes))
        for probe in task.contamination_probes:
            ctx = ProverContext(
                task=task,
                job=job,
                phase="probe",
                system=PROBE_SYSTEM_PROMPT,
                user=probe_prompt(task, probe.id),
                allowed_hosts=self.allowed_hosts,
                probe_id=probe.id,
                log_dir=Path(log_dir) if log_dir is not None else None,
            )
            t0 = time.perf_counter()
            try:
                resp = self.backend.run(ctx)
            except Exception as exc:  # noqa: BLE001 - record and surface
                result.error = f"探针 {probe.id}: {type(exc).__name__}: {exc}"
                log.error("job %s: 探针阶段异常: %s", job.job_id, result.error)
                return
            pr = evaluate_probe(probe, resp.text)
            if not pr.leaked and self.probe_judge is not None:
                # 关键词 clean → 语义评审二查 (换措辞的实质复现)。
                try:
                    sem_leaked, sem_notes = self.probe_judge.assess(task, probe, resp.text)
                except Exception as exc:  # noqa: BLE001 - 语义评审异常不应中断探针
                    sem_leaked, sem_notes = False, f"语义评审异常: {type(exc).__name__}: {exc}"
                    log.warning("job %s: 探针 %s 语义评审异常: %s", job.job_id, probe.id, sem_notes)
                pr.semantic_leak = sem_leaked
                pr.semantic_notes = sem_notes
                if sem_leaked:
                    pr.leaked = True
            result.probe_responses.append(pr)
            result.usage.input_tokens += resp.usage.input_tokens
            result.usage.output_tokens += resp.usage.output_tokens
            result.usage.wall_seconds += resp.usage.wall_seconds or (time.perf_counter() - t0)
            log.info(
                "  探针 %s (%s): %s%s%s",
                probe.id, probe.kind.value,
                "❌ 泄露" if pr.leaked else "✅ clean",
                f" 命中={pr.matched_indicators}" if pr.matched_indicators else "",
                " [语义]" if pr.semantic_leak else "",
            )

    def run_probes(self, task: TaskSpec, job: Job) -> ProverRunResult:
        """Probe-only entry (CLI ``probe`` 子命令): never enters the prove phase."""
        result = self._new_result(task, job)
        self._probe_phase(task, job, result)
        if any_leaked(result.probe_responses):
            result.contaminated = True
            log.warning("job %s: 探针命中关键创新 → 判污染除名", job.job_id)
        return result

    def run(
        self,
        task: TaskSpec,
        job: Job,
        probe_responses: list | None = None,
        log_dir: str | Path | None = None,
    ) -> ProverRunResult:
        result = self._new_result(task, job)

        # 1. Probe phase ------------------------------------------------- #
        # Contamination is a property of (model, task), not of the hint level,
        # so the Controller probes once and passes the cached responses here
        # (plan §8.3). Only run probes when none were supplied.
        if probe_responses is None:
            self._probe_phase(task, job, result, log_dir=log_dir)
        else:
            result.probe_responses = list(probe_responses)
            log.debug("job %s: 复用缓存探针 (%d)", job.job_id, len(probe_responses))

        if any_leaked(result.probe_responses):
            # 探针命中 → 该 (model, task) 判污染, 不计入正式分 (plan §3.3, §8.3).
            # 即使探针电池因异常不完整, 已有的泄露证据也成立。
            result.contaminated = True
            log.warning("job %s: 探针命中关键创新 → 判污染除名", job.job_id)
            return result
        if result.error is not None:
            # 探针电池不完整 (基础设施错误): 无法判定 clean, 不进入证明阶段。
            return result

        # 2. Prove phase ------------------------------------------------- #
        ctx = ProverContext(
            task=task,
            job=job,
            phase="prove",
            system=PROVER_SYSTEM_PROMPT,
            user=prove_prompt(task, job.hint_level),
            allowed_hosts=self.allowed_hosts,
            log_dir=Path(log_dir) if log_dir is not None else None,
        )
        log.info("job %s: 证明阶段 (hint L%d)", job.job_id, job.hint_level)
        t0 = time.perf_counter()
        try:
            resp = self.backend.run(ctx)
        except Exception as exc:  # noqa: BLE001 - record and surface
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("job %s: 证明阶段异常: %s", job.job_id, result.error)
            return result
        prove_elapsed = time.perf_counter() - t0

        if not resp.text.strip() and resp.structured is None:
            result.error = "EmptyOutput: 证明阶段返回空文本"
            log.error("job %s: 证明阶段空输出", job.job_id)
            return result

        result.raw_output = resp.text
        result.structured_output = resp.structured or parse_structured(resp.text)
        result.transcript = resp.tool_calls
        result.usage.input_tokens += resp.usage.input_tokens
        result.usage.output_tokens += resp.usage.output_tokens
        result.usage.wall_seconds += resp.usage.wall_seconds or prove_elapsed
        log.info(
            "job %s: 证明完成 %.1fs, %d 字, 检索 %d 次, tok(in/out)=%d/%d",
            job.job_id, prove_elapsed, len(result.proof_text),
            len(result.transcript), resp.usage.input_tokens, resp.usage.output_tokens,
        )

        # 3. Audit ------------------------------------------------------- #
        result.audit = audit_run(
            transcript=result.transcript,
            cutoff=task.retrieval_cutoff,
            allowed_hosts=self.allowed_hosts,
        )
        if not result.audit.passed:
            log.warning(
                "job %s: 审计失败 越界引用=%s 越界网络=%s",
                job.job_id, result.audit.out_of_window_references,
                result.audit.out_of_window_network,
            )
        else:
            log.debug("job %s: 审计通过 (引用均 <= cutoff)", job.job_id)
        return result
