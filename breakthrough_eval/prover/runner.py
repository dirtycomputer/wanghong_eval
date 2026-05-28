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

import re

from ..arxiv_frozen import ArxivFrozenSource
from ..contamination import any_leaked, audit_run, evaluate_probe
from ..hints import PROVER_SYSTEM_PROMPT, probe_prompt, prove_prompt
from ..models import (
    Job,
    ProverRunResult,
    ProverStructuredOutput,
    TaskSpec,
)
from .base import ProverBackend, ProverContext
from .mock import ARXIV_MCP_HOST

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
        source: ArxivFrozenSource,
        allowed_hosts: set[str] | None = None,
    ):
        self.backend = backend
        self.source = source
        self.allowed_hosts = allowed_hosts or {ARXIV_MCP_HOST}

    def run(self, task: TaskSpec, job: Job) -> ProverRunResult:
        harness = self.backend.harness_fingerprint(job.model)
        result = ProverRunResult(
            job_id=job.job_id,
            task_id=task.task_id,
            model=job.model,
            hint_level=job.hint_level,
            trial=job.trial,
            harness=harness,
        )

        # 1. Probe phase ------------------------------------------------- #
        for probe in task.contamination_probes:
            ctx = ProverContext(
                task=task,
                job=job,
                phase="probe",
                system=PROVER_SYSTEM_PROMPT,
                user=probe_prompt(task, probe.id),
                source=self.source,
                allowed_hosts=self.allowed_hosts,
                probe_id=probe.id,
            )
            resp = self.backend.run(ctx)
            result.probe_responses.append(evaluate_probe(probe, resp.text))
            result.usage.input_tokens += resp.usage.input_tokens
            result.usage.output_tokens += resp.usage.output_tokens
            result.usage.wall_seconds += resp.usage.wall_seconds

        if any_leaked(result.probe_responses):
            # 探针命中 → 该 (model, task) 判污染, 不计入正式分 (plan §3.3, §8.3).
            result.contaminated = True
            return result

        # 2. Prove phase ------------------------------------------------- #
        ctx = ProverContext(
            task=task,
            job=job,
            phase="prove",
            system=PROVER_SYSTEM_PROMPT,
            user=prove_prompt(task, job.hint_level),
            source=self.source,
            allowed_hosts=self.allowed_hosts,
        )
        try:
            resp = self.backend.run(ctx)
        except Exception as exc:  # noqa: BLE001 - record and surface
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        result.raw_output = resp.text
        result.structured_output = resp.structured or parse_structured(resp.text)
        result.transcript = resp.tool_calls
        result.usage.input_tokens += resp.usage.input_tokens
        result.usage.output_tokens += resp.usage.output_tokens
        result.usage.wall_seconds += resp.usage.wall_seconds

        # 3. Audit ------------------------------------------------------- #
        result.audit = audit_run(
            cited_references=result.structured_output.cited_references,
            transcript=result.transcript,
            source=self.source,
            cutoff=task.retrieval_cutoff,
            allowed_hosts=self.allowed_hosts,
        )
        return result
