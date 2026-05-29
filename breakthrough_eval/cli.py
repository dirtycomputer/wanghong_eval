"""Command-line interface for the breakthrough-eval system.

Subcommands:
  validate     校验一个/一批 TaskSpec
  list-tasks   列出已加载的 task
  list-models  列出 registry 里的 model 及其对某 task 的资格
  run          展开 job 矩阵并跑通 Controller→PROVER→EVAL→分数, 落盘
  leaderboard  从 results 目录聚合并渲染榜单 + 难度曲线
  probe        只跑污染探针 (probe-then 的 probe 段)
  diff-check   差分 sanity check (冻结 vs 突破后 arXiv)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .controller import Controller
from .eval.base import JudgeBackend
from .eval.evaluator import Evaluator
from .eval.mock import MockJudge, MockJudgeConfig
from .eval.openrouter_judge import openrouter_judge
from .leaderboard import Leaderboard
from .registry import ModelRegistry
from .storage import ResultStore
from .taskspec import load_all_tasks, validate_taskspec

DEFAULT_TASKS_DIR = "tasks"
DEFAULT_REGISTRY = "models_registry.yaml"
DEFAULT_RESULTS = "results"


def _default_judges() -> list[JudgeBackend]:
    # A 3-judge panel with differing strictness → realistic agreement (plan §4.2).
    return [
        MockJudge(MockJudgeConfig(strictness=0.4, name="judge-lenient")),
        MockJudge(MockJudgeConfig(strictness=0.5, name="judge-balanced")),
        MockJudge(MockJudgeConfig(strictness=0.7, name="judge-strict")),
    ]


def _parse_judges(spec: str | None) -> list[JudgeBackend]:
    """Parse a ``--judges`` spec into judge backends.

    Comma-separated entries; each is one of:
      * ``mock`` or ``mock:<strictness>``
      * ``openrouter:<model>``  e.g. ``openrouter:deepseek/deepseek-v4-pro``
    Empty / None → the default 3-judge mock panel.
    """
    if not spec:
        return _default_judges()
    judges: list[JudgeBackend] = []
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if entry == "mock":
            judges.append(MockJudge(MockJudgeConfig(name="mock")))
        elif entry.startswith("mock:"):
            judges.append(
                MockJudge(MockJudgeConfig(strictness=float(entry.split(":", 1)[1]), name=entry))
            )
        elif entry.startswith("openrouter:"):
            model = entry.split(":", 1)[1]
            judges.append(openrouter_judge(model))
        else:
            raise SystemExit(f"未知 judge spec: '{entry}' (用 mock / mock:0.5 / openrouter:<model>)")
    return judges


def _build_controller(args) -> Controller:
    tasks = load_all_tasks(args.tasks_dir)
    registry = ModelRegistry.load(args.registry)
    evaluator = Evaluator(_parse_judges(getattr(args, "judges", None)))
    store = ResultStore(args.results_dir)
    return Controller(tasks=tasks, registry=registry, evaluator=evaluator, store=store)


# --------------------------------------------------------------------------- #
def cmd_validate(args) -> int:
    paths = (
        [Path(args.path)]
        if Path(args.path).is_file()
        else sorted(Path(args.path).glob("*.y*ml"))
    )
    rc = 0
    for p in paths:
        issues = validate_taskspec(p)
        hard = [i for i in issues if not i.startswith("⚠️")]
        if hard:
            print(f"❌ {p}: 无效\n   " + "\n   ".join(hard))
            rc = 1
        else:
            print(f"✅ {p}: 通过红线校验")
            for w in issues:
                print(f"   {w}")
    return rc


def cmd_list_tasks(args) -> int:
    tasks = load_all_tasks(args.tasks_dir)
    for t in tasks.values():
        print(
            f"- {t.task_id}: {t.title}\n"
            f"    breakthrough={t.breakthrough_date} retrieval_cutoff={t.retrieval_cutoff} "
            f"model_cutoff_before={t.allowed_model_cutoff_before}\n"
            f"    rubric={len(t.rubric)} probes={len(t.contamination_probes)} "
            f"hints=L0..L{t.max_hint_level}"
        )
    return 0


def cmd_list_models(args) -> int:
    tasks = load_all_tasks(args.tasks_dir)
    registry = ModelRegistry.load(args.registry)
    task = tasks[args.task] if args.task else None
    for e in registry.entries.values():
        line = (
            f"- {e.name}: cutoff={e.cutoff_date} ({e.cutoff_confidence}) "
            f"open_source={e.open_source} tier={e.capability_tier} provider={e.provider}"
        )
        if task is not None:
            ok = e.eligible_for(task)
            line += f"  → {'✅ 合格' if ok else '❌ 不合格'} for {task.task_id}"
        print(line)
    return 0


def cmd_run(args) -> int:
    controller = _build_controller(args)
    task_ids = [args.task] if args.task else list(controller.tasks)
    models = args.models.split(",") if args.models else None
    hints = [int(x) for x in args.hints.split(",")] if args.hints else None

    matrix = controller.expand_job_matrix(
        task_ids=task_ids, model_names=models, hint_levels=hints, trials=args.trials
    )
    print(f"展开 {len(matrix.jobs)} 个 job; 跳过 {len(matrix.skipped)} 个:")
    for tid, m, reason in matrix.skipped:
        print(f"  · skip {tid}/{m}: {reason}")

    results = controller.run(matrix, early_stop_on_contamination=not args.no_early_stop)
    contaminated = sum(1 for pr, _ in results if pr.contaminated)
    valid = sum(1 for _, ev in results if ev.overall_valid and not ev.excluded)
    print(f"\n完成 {len(results)} 个 run: {valid} 个整体有效, {contaminated} 个判污染。")
    print(f"产物已落盘到 {args.results_dir}/。运行 `leaderboard` 查看榜单。")

    if matrix.skipped:
        # report post-run early-stop skips too
        pass
    return 0


def cmd_leaderboard(args) -> int:
    store = ResultStore(args.results_dir)
    prover = store.load_prover_all()
    evals = store.load_eval_all()
    if not prover:
        print(f"results 目录 {args.results_dir} 为空, 先跑 `run`。")
        return 1
    board = Leaderboard.build(prover, evals)
    print("# 主榜 (按 hint-AUC 排序)\n")
    print(board.render_main_table())
    print("\n# 难度曲线 (每个 harness 一条 solve_rate vs hint)\n")
    for row in board.rows:
        if row.removed:
            continue
        print(board.render_difficulty_curve(row.harness, row.task_id))
        print()
    return 0


def cmd_probe(args) -> int:
    from .models import Job
    from .prover.runner import ProverRunner

    controller = _build_controller(args)
    task = controller.tasks[args.task]
    backend = controller.backend_for(args.model)
    source = controller.source_factory(task.retrieval_cutoff)
    runner = ProverRunner(backend, source)
    job = Job(task_id=args.task, model=args.model, hint_level=0, trial=0)
    result = runner.run(task, job)
    print(f"探针结果 — {args.model} @ {args.task}:")
    for pr in result.probe_responses:
        flag = "❌ 泄露" if pr.leaked else "✅ 干净"
        hits = f" 命中:{pr.matched_indicators}" if pr.matched_indicators else ""
        print(f"  [{flag}] {pr.probe_id} ({pr.kind.value}){hits}")
    print("结论:", "污染除名" if result.contaminated else "探针洁净, 可计入正式分")
    return 0


def cmd_diff_check(args) -> int:
    controller = _build_controller(args)
    report = controller.differential_sanity_check(
        args.task, args.model, hint_level=args.hint, trials=args.trials
    )
    print("差分 sanity check (验证度量在测「独立性」而非检索):")
    for k, v in report.items():
        print(f"  {k}: {v}")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="breakthrough-eval", description=__doc__)
    p.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR)
    p.add_argument("--registry", default=DEFAULT_REGISTRY)
    p.add_argument("--results-dir", default=DEFAULT_RESULTS)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="校验 TaskSpec")
    v.add_argument("path", nargs="?", default=DEFAULT_TASKS_DIR)
    v.set_defaults(func=cmd_validate)

    lt = sub.add_parser("list-tasks", help="列出 task")
    lt.set_defaults(func=cmd_list_tasks)

    lm = sub.add_parser("list-models", help="列出 model 及资格")
    lm.add_argument("--task", default=None)
    lm.set_defaults(func=cmd_list_models)

    r = sub.add_parser("run", help="跑通 Controller→PROVER→EVAL")
    r.add_argument("--task", default=None, help="task_id (默认全部)")
    r.add_argument("--models", default=None, help="逗号分隔 (默认全部合格 model)")
    r.add_argument("--hints", default=None, help="逗号分隔 hint level, e.g. 0,1,2 (默认全部)")
    r.add_argument("--trials", type=int, default=3)
    r.add_argument("--no-early-stop", action="store_true")
    r.add_argument(
        "--judges",
        default=None,
        help="评委: mock / mock:0.5 / openrouter:<model> (逗号分隔; 默认 mock 三评委面板)",
    )
    r.set_defaults(func=cmd_run)

    lb = sub.add_parser("leaderboard", help="渲染榜单")
    lb.set_defaults(func=cmd_leaderboard)

    pr = sub.add_parser("probe", help="只跑污染探针")
    pr.add_argument("--task", required=True)
    pr.add_argument("--model", required=True)
    pr.set_defaults(func=cmd_probe)

    dc = sub.add_parser("diff-check", help="差分 sanity check")
    dc.add_argument("--task", required=True)
    dc.add_argument("--model", required=True)
    dc.add_argument("--hint", type=int, default=0)
    dc.add_argument("--trials", type=int, default=3)
    dc.add_argument("--judges", default=None, help="同 run --judges")
    dc.set_defaults(func=cmd_diff_check)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
