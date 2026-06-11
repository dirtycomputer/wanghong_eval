"""评审修复批次: 探针确定性/共享缓存/语义化, golden 注入, earned coverage."""

import json

from breakthrough_eval.contamination import SemanticProbeJudge
from breakthrough_eval.controller import Controller
from breakthrough_eval.eval.evaluator import Evaluator, golden_reference_text
from breakthrough_eval.eval.llm import build_judge_prompt
from breakthrough_eval.eval.mock import MockJudge
from breakthrough_eval.hints import probe_prompt, prove_prompt
from breakthrough_eval.models import Job, ModelEntry
from breakthrough_eval.prover.base import ProverContext
from breakthrough_eval.prover.mock import MockProverBackend, MockProverConfig
from breakthrough_eval.prover.runner import ProverRunner
from breakthrough_eval.arxiv_frozen import InMemoryArxivSource
from breakthrough_eval.registry import ModelRegistry


# --------------------------------------------------------------------------- #
# 1) 语义级探针评审: 关键词是下限, 语义抓换措辞复现
# --------------------------------------------------------------------------- #
def _runner(task, probe_judge=None, **cfg):
    backend = MockProverBackend(MockProverConfig(**cfg))
    return ProverRunner(backend, InMemoryArxivSource(task.retrieval_cutoff),
                        probe_judge=probe_judge)


def _job(task):
    return Job(task_id=task.task_id, model="m", hint_level=0, trial=0)


def test_semantic_probe_judge_catches_paraphrase(task):
    # mock clean 模型关键词全过, 但语义评审判定其实质复现 → 判污染。
    judge = SemanticProbeJudge(
        complete=lambda s, u: json.dumps(
            {"leaked": True, "evidence": "换措辞复现了体积估计路线", "confidence": 0.9}
        )
    )
    res = _runner(task, probe_judge=judge).run_probes(task, _job(task))
    assert res.contaminated
    assert all(p.semantic_leak and p.leaked for p in res.probe_responses)
    assert "换措辞" in res.probe_responses[0].semantic_notes


def test_semantic_probe_judge_abstains_on_garbage(task):
    # 语义评审输出无法解析 → 弃权 (不冤杀), 关键词 clean 维持 clean, 留痕。
    judge = SemanticProbeJudge(complete=lambda s, u: "not json at all")
    res = _runner(task, probe_judge=judge).run_probes(task, _job(task))
    assert not res.contaminated
    assert all(not p.leaked and "弃权" in p.semantic_notes for p in res.probe_responses)


def test_keyword_hit_short_circuits_semantic_judge(task):
    # 关键词一票否决在前: 已命中就不再花语义评审的调用。
    calls = {"n": 0}

    def counting(system, user):
        calls["n"] += 1
        return json.dumps({"leaked": False})

    judge = SemanticProbeJudge(complete=counting)
    res = _runner(task, probe_judge=judge, contaminated=True).run_probes(task, _job(task))
    assert res.contaminated
    assert calls["n"] == 0


def test_semantic_judge_exception_does_not_break_probe(task):
    def boom(system, user):
        raise RuntimeError("network down")

    res = _runner(task, probe_judge=SemanticProbeJudge(complete=boom)).run_probes(
        task, _job(task))
    assert not res.contaminated  # 异常 → 弃权, 不中断、不冤杀


# --------------------------------------------------------------------------- #
# 2) 探针缓存按底层权重共享 (同一模型挂多个 harness 只探一次)
# --------------------------------------------------------------------------- #
def _entry(name, **kw):
    return ModelEntry(
        name=name, cutoff_date="2024-10-01", provider="mock",
        backend_kwargs={"model": "shared-weights", "capability": 0.6, **kw},
    )


def _counting_backend(counter):
    backend = MockProverBackend(MockProverConfig(capability=0.6))
    orig = backend.run

    def counting(ctx: ProverContext):
        if ctx.phase == "probe":
            counter["n"] += 1
        return orig(ctx)

    backend.run = counting
    return backend


def test_probe_cache_shared_across_harnesses(tasks, task):
    registry = ModelRegistry([_entry("w-harness-a"), _entry("w-harness-b")])
    ctrl = Controller(tasks, registry, Evaluator([MockJudge()]))
    counter = {"n": 0}
    ctrl._backend_cache["w-harness-a"] = _counting_backend(counter)
    ctrl._backend_cache["w-harness-b"] = _counting_backend(counter)
    matrix = ctrl.expand_job_matrix(
        [task.task_id], model_names=["w-harness-a", "w-harness-b"],
        hint_levels=[0], trials=2,
    )
    ctrl.run(matrix)
    # 两个 harness 共享 backend_kwargs.model="shared-weights" → 整个矩阵只探一轮。
    assert counter["n"] == len(task.contamination_probes)


def test_shared_weights_contamination_early_stops_all_harnesses(tasks, task):
    registry = ModelRegistry([
        _entry("leaky-a", contaminated=True), _entry("leaky-b", contaminated=True),
    ])
    ctrl = Controller(tasks, registry, Evaluator([MockJudge()]))
    matrix = ctrl.expand_job_matrix(
        [task.task_id], model_names=["leaky-a", "leaky-b"], hint_levels=[0, 1], trials=1,
    )
    results = ctrl.run(matrix, early_stop_on_contamination=True)
    # 每组只跑 prime 即判污染; 第二组复用共享缓存, 同样除名。
    assert len(results) == 2 and all(pr.contaminated for pr, _ in results)


# --------------------------------------------------------------------------- #
# 3) golden 全文注入评委 prompt; 绝不进 PROVER 提示
# --------------------------------------------------------------------------- #
def test_judge_prompt_includes_golden_proof_text(task):
    user = build_judge_prompt(task, "proof", task.golden_proof)
    assert "Golden 证明概要/全文" in user
    assert "Wang–Zahl 证明结构概要" in user


def test_judge_prompt_flags_missing_golden_text(task):
    golden = task.golden_proof.model_copy(update={"proof_text": ""})
    user = build_judge_prompt(task, "proof", golden)
    assert "未提供 golden 证明全文" in user


def test_golden_proof_text_never_reaches_prover(task):
    marker = "Wang–Zahl 证明结构概要"
    assert marker in task.golden_proof.proof_text
    for level in [h.level for h in task.hint_ladder]:
        assert marker not in prove_prompt(task, level)
    for p in task.contamination_probes:
        assert marker not in probe_prompt(task, p.id)


# --------------------------------------------------------------------------- #
# 4) earned coverage: hint 白送的项不算挣到的
# --------------------------------------------------------------------------- #
def test_earned_coverage_excludes_revealed_items(task):
    ev = Evaluator([MockJudge()])
    # 满分文本 + L3 (累积揭示 R1,R2,R3,R4) → earned 只数 R5,R6,R7。
    res = ev.evaluate_text("j", task, golden_reference_text(task), hint_level=3)
    assert res.passed_items == 7
    assert sorted(res.revealed_items) == ["R1", "R2", "R3", "R4"]
    assert res.earned_total_items == 3 and res.earned_passed_items == 3
    assert res.earned_coverage == 1.0


def test_earned_coverage_undefined_when_all_revealed(task):
    ev = Evaluator([MockJudge()])
    res = ev.evaluate_text("j", task, golden_reference_text(task), hint_level=5)
    assert res.earned_total_items == 0
    assert res.earned_coverage is None


def test_cold_start_earned_equals_full_coverage(task):
    ev = Evaluator([MockJudge()])
    res = ev.evaluate_text("j", task, golden_reference_text(task), hint_level=0)
    assert res.earned_total_items == res.total_items
    assert res.earned_coverage == res.rubric_coverage == 1.0