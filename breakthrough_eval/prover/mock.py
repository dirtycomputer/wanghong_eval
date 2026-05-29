"""Deterministic mock PROVER backend (default, offline-runnable).

It simulates a pre-cutoff research model:

  * **probe phase** — a *clean* model answers without leaking the key
    innovation, so probes pass. Set ``contaminated=True`` to simulate a model
    that leaks (its probe answers will hit the spec's ``leak_indicators`` and the
    run gets excluded, exercising the §8.3 path).
  * **prove phase** — produces a structured writeup whose rubric coverage scales
    with the model's ``capability`` and the hint level (plan §5). Hints
    explicitly reveal certain rubric items (``reveals_rubric_items``); the rest
    must be "solved" by capability, with frontier-delta items being harder.

Everything is seeded from the job id, so runs are reproducible yet vary across
trials (needed for pass@k / multi-trial, plan §4.3).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from random import Random

from ..arxiv_frozen import ArxivFrozenSource
from ..models import ProverStructuredOutput, TaskSpec, ToolCall, UsageStats
from .base import BackendResponse, ProverBackend, ProverContext

ARXIV_MCP_HOST = "arxiv-frozen-mcp.internal"
_ARXIV_ID_RE = re.compile(r"([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})", re.I)


def _golden_retrievable(task: TaskSpec, source: ArxivFrozenSource) -> bool:
    """True iff the golden paper's arXiv id is reachable under the source cutoff."""
    m = _ARXIV_ID_RE.search(task.golden_proof.primary)
    if not m:
        return False
    return source.get(m.group(1)) is not None


@dataclass
class MockProverConfig:
    capability: float = 0.5  # base research ability in [0, 1]
    contaminated: bool = False  # simulate a leaked model (fails probes)
    scaffold_version: str = "vanilla"


class MockProverBackend(ProverBackend):
    name = "mock"

    def __init__(self, config: MockProverConfig | None = None):
        self.config = config or MockProverConfig()
        self.scaffold_version = self.config.scaffold_version

    # ------------------------------------------------------------------ #
    def run(self, ctx: ProverContext) -> BackendResponse:
        if ctx.phase == "probe":
            return self._run_probe(ctx)
        return self._run_prove(ctx)

    # ---- probe phase -------------------------------------------------- #
    def _run_probe(self, ctx: ProverContext) -> BackendResponse:
        probe = next(
            (p for p in ctx.task.contamination_probes if p.id == ctx.probe_id), None
        )
        usage = UsageStats(input_tokens=120, output_tokens=60, wall_seconds=0.4)
        if self.config.contaminated and probe is not None:
            # A leaked model parrots the post-T key innovation.
            leak = "; ".join(probe.leak_indicators)
            text = (
                "据我所知, 这个问题已经被解决了。关键在于: " + leak + "。"
                "这套结构定理把所有情形闭合从而得到满维结论。"
            )
        else:
            text = (
                "据我所知, 截止我的知识范围, 这是一个公认的公开未解决问题。"
                "我不清楚是否已有完整证明, 也无法凭空给出突破性的解法。"
            )
        return BackendResponse(text=text, usage=usage)

    # ---- prove phase -------------------------------------------------- #
    def _run_prove(self, ctx: ProverContext) -> BackendResponse:
        task, job = ctx.task, ctx.job
        # Stable, cross-process seed (built-in hash() is salted per process).
        seed = int(hashlib.sha256(job.job_id.encode()).hexdigest()[:8], 16)
        rng = Random(seed)

        max_level = task.max_hint_level or 1
        hint_fraction = job.hint_level / max_level if max_level else 0.0

        # Items the hints explicitly hand over (cumulative up to this level).
        revealed: set[str] = set()
        for h in task.hint_ladder:
            if h.level <= job.hint_level:
                revealed.update(h.reveals_rubric_items)

        cap = max(0.0, min(1.0, self.config.capability))

        # If the golden breakthrough paper is *within the retrieval window*, the
        # model can simply read and copy the answer (simulates retrieval-based
        # contamination). The frozen source hides it; a deliberately-leaky
        # post-breakthrough source exposes it — this is what the §8 differential
        # sanity check detects.
        answer_in_reach = _golden_retrievable(task, ctx.source)

        # For each item compute a "quality" in [0,1] and emit that fraction of
        # its indicators. Partial emission is what makes strict vs lenient judges
        # disagree (plan §4.2). Revealed items are fully handed over.
        emitted: dict[str, list[str]] = {}
        for item in task.rubric:
            if item.id in revealed or answer_in_reach:
                q = 1.0
            elif item.frontier_delta:
                q = cap * (0.15 + 0.85 * hint_fraction)  # the contested delta: hard
            else:
                q = cap * (0.55 + 0.45 * hint_fraction)  # legal pre-T tooling: easier
            q = max(0.0, min(1.0, q + rng.uniform(-0.12, 0.12)))
            n = round(q * len(item.indicators))
            if n > 0:
                emitted[item.id] = item.indicators[:n]

        # Retrieve legal pre-T references from the frozen source.
        tool_calls, citations = self._retrieve(ctx, rng)

        structured = self._writeup(task, emitted, citations)
        usage = UsageStats(
            input_tokens=800 + 200 * job.hint_level,
            output_tokens=400 + 120 * len(emitted),
            wall_seconds=2.0 + 0.5 * len(emitted),
        )
        return BackendResponse(
            text=structured.as_text(),
            tool_calls=tool_calls,
            usage=usage,
            structured=structured,
        )

    # ------------------------------------------------------------------ #
    def _retrieve(self, ctx: ProverContext, rng: Random):
        # Query the frozen arXiv with terms from the problem statement.
        query = "kakeya tube incidence polynomial method induction scales"
        papers = ctx.source.search(query, max_results=4)
        tool_calls: list[ToolCall] = []
        citations: list[str] = []
        for p in papers:
            tool_calls.append(
                ToolCall(
                    tool="arxiv_frozen.search",
                    query=query,
                    host=ARXIV_MCP_HOST,
                    returned_dates=[p.submission_date],
                )
            )
            citations.append(p.citation)
        return tool_calls, citations

    def _writeup(self, task, emitted: dict, citations) -> ProverStructuredOutput:
        # The lemma block is the machine-checkable evidence: one line per item
        # carrying exactly the emitted indicator phrases. Prose elsewhere is kept
        # free of indicator words so the judge scores only this evidence.
        overview = "本证明尝试沿离散化 + 逐尺度论证组织, 处理若干关键环节。"
        if emitted:
            # Emit only the id + the actually-derived indicator phrases. The
            # rubric *title* is deliberately NOT echoed (titles can contain
            # indicator phrasing, which would inflate the judge's match count).
            lemma_lines = [
                f"- 引理 {iid}: " + ", ".join(inds) for iid, inds in emitted.items()
            ]
        else:
            lemma_lines = ["- (未能给出关键引理)"]
        proof = (
            "我们尝试把以上各引理拼接为对原命题的论证; 凡未给出引理的环节均为缺口。"
        )
        return ProverStructuredOutput(
            idea_overview=overview,
            key_lemmas="\n".join(lemma_lines),
            full_proof=proof,
            cited_references=citations,
        )


def _factory(**kwargs) -> MockProverBackend:
    cfg = MockProverConfig(
        capability=kwargs.get("capability", 0.5),
        contaminated=kwargs.get("contaminated", False),
        scaffold_version=kwargs.get("scaffold_version", "vanilla"),
    )
    return MockProverBackend(cfg)


# Registered in breakthrough_eval.__init__ to avoid import cycles at module load.
