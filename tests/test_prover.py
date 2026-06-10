"""PROVER probe-then-prove behaviour (plan §3)."""

from datetime import date

from breakthrough_eval.arxiv_frozen import InMemoryArxivSource
from breakthrough_eval.models import Job
from breakthrough_eval.prover.mock import MockProverBackend, MockProverConfig
from breakthrough_eval.prover.runner import ProverRunner, parse_structured


def _runner(task, **cfg):
    backend = MockProverBackend(MockProverConfig(**cfg))
    return ProverRunner(backend, InMemoryArxivSource(task.retrieval_cutoff))


def test_clean_model_passes_probes_and_proves(task):
    runner = _runner(task, capability=0.7)
    res = runner.run(task, Job(task_id=task.task_id, model="m", hint_level=5, trial=0))
    assert not res.contaminated
    assert not res.excluded
    assert res.structured_output is not None
    assert res.structured_output.cited_references


def test_contaminated_model_is_excluded_and_skips_prove(task):
    runner = _runner(task, capability=0.7, contaminated=True)
    res = runner.run(task, Job(task_id=task.task_id, model="m", hint_level=0, trial=0))
    assert res.contaminated
    assert res.excluded
    assert res.structured_output is None  # prove phase skipped
    assert any(p.leaked for p in res.probe_responses)


def test_all_citations_within_cutoff(task):
    runner = _runner(task, capability=0.8)
    res = runner.run(task, Job(task_id=task.task_id, model="m", hint_level=3, trial=1))
    assert res.audit.passed
    src = InMemoryArxivSource(task.retrieval_cutoff)
    for call in res.transcript:
        for d in call.returned_dates:
            assert d <= task.retrieval_cutoff


def test_higher_hints_yield_more_evidence(task):
    runner = _runner(task, capability=0.5)
    low = runner.run(task, Job(task_id=task.task_id, model="m", hint_level=0, trial=0))
    high = runner.run(task, Job(task_id=task.task_id, model="m", hint_level=5, trial=0))
    assert len(high.proof_text) >= len(low.proof_text)


def test_reproducible(task):
    r1 = _runner(task, capability=0.6).run(
        task, Job(task_id=task.task_id, model="m", hint_level=2, trial=0)
    )
    r2 = _runner(task, capability=0.6).run(
        task, Job(task_id=task.task_id, model="m", hint_level=2, trial=0)
    )
    assert r1.proof_text == r2.proof_text


def test_run_probes_only_never_enters_prove_phase(task):
    # `probe` 子命令的入口: 只跑探针电池, 绝不触发 (昂贵的) 证明阶段。
    backend = MockProverBackend(MockProverConfig(capability=0.7))
    prove_calls = {"n": 0}
    orig = backend.run

    def counting(ctx):
        if ctx.phase == "prove":
            prove_calls["n"] += 1
        return orig(ctx)

    backend.run = counting
    runner = ProverRunner(backend, InMemoryArxivSource(task.retrieval_cutoff))
    res = runner.run_probes(task, Job(task_id=task.task_id, model="m", hint_level=0, trial=0))
    assert len(res.probe_responses) == len(task.contamination_probes)
    assert not res.contaminated
    assert prove_calls["n"] == 0
    assert res.structured_output is None and res.raw_output == ""


def test_run_probes_flags_contamination(task):
    runner = _runner(task, capability=0.7, contaminated=True)
    res = runner.run_probes(task, Job(task_id=task.task_id, model="m", hint_level=0, trial=0))
    assert res.contaminated


def test_probe_phase_error_recorded_not_raised(task):
    # 探针阶段的后端异常 (网络/provider) 记录为 error 而非向上炸掉整个批次;
    # 电池不完整 → 无法判定 clean → 不进证明阶段。
    class Boom(MockProverBackend):
        def run(self, ctx):
            if ctx.phase == "probe":
                raise RuntimeError("network down")
            return super().run(ctx)

    runner = ProverRunner(Boom(MockProverConfig()), InMemoryArxivSource(task.retrieval_cutoff))
    res = runner.run(task, Job(task_id=task.task_id, model="m", hint_level=0, trial=0))
    assert res.error is not None and "network down" in res.error
    assert not res.contaminated
    assert res.structured_output is None and res.raw_output == ""


def test_parse_structured_roundtrip():
    text = (
        "# Claimed Proof\n## 1. 思路总览\nidea\n## 2. 关键引理与论证\nlemmas\n"
        "## 3. 完整证明\nproof\n## 4. 引用文献\n- arXiv:1409.1885\n"
    )
    parsed = parse_structured(text)
    assert parsed.idea_overview == "idea"
    assert parsed.cited_references == ["arXiv:1409.1885"]
