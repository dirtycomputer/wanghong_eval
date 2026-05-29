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


def test_parse_structured_roundtrip():
    text = (
        "# Claimed Proof\n## 1. 思路总览\nidea\n## 2. 关键引理与论证\nlemmas\n"
        "## 3. 完整证明\nproof\n## 4. 引用文献\n- arXiv:1409.1885\n"
    )
    parsed = parse_structured(text)
    assert parsed.idea_overview == "idea"
    assert parsed.cited_references == ["arXiv:1409.1885"]
