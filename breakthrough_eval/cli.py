"""Command-line interface for the breakthrough-eval system.

Subcommands:
  validate     校验一个/一批 TaskSpec
  list-tasks   列出已加载的 task
  list-models  列出 registry 里的 model 及其对某 task 的资格
  run          展开 job 矩阵并跑通 Controller→PROVER→EVAL→分数, 落盘
  leaderboard  从 results 目录聚合并渲染榜单 + 难度曲线
  export-web   导出可视化站点数据 (docs/data.js, 供 GitHub Pages)
  probe        只跑污染探针 (probe-then 的 probe 段)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .controller import Controller
from .eval.evaluator import Evaluator
from .leaderboard import Leaderboard
from .logging_util import setup_logging
from .specification import ModelRegistry
from .storage import ResultStore
from .specification import load_all_tasks, validate_taskspec

DEFAULT_TASKS_DIR = "tasks"
DEFAULT_REGISTRY = "models_registry.yaml"
DEFAULT_RESULTS = "results"
DEFAULT_EVAL_CONFIG = "breakthrough_eval/eval/eval.yaml"


def _build_controller(args) -> Controller:
    tasks = load_all_tasks(args.tasks_dir)
    registry = ModelRegistry.load(args.registry)
    store = ResultStore(args.results_dir)
    evaluator = Evaluator.load(store.eval_dir, getattr(args, "eval_config", None))
    return Controller(
        tasks=tasks, registry=registry, evaluator=evaluator, store=store,
        probe_judge=None,
    )


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
        api_model = e.api.model if e.api is not None else "—"
        line = (
            f"- {e.name}: cutoff={e.cutoff_date} ({e.cutoff_confidence}) "
            f"open_source={e.open_source} tier={e.capability_tier} "
            f"harness={e.harness.type} api_model={api_model}"
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
    n_pre_skips = len(matrix.skipped)

    # 运行配置落盘 (供 export-web / 前端「运行配置」展示; 跑挂了也留有现场)。
    from datetime import datetime, timezone

    controller.store.save_run_meta({
        "kind": "breakthrough-eval.run",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "registry": args.registry,
        "task_ids": task_ids,
        "models": models,  # null = 全部合格 model
        "hint_levels": hints,  # null = 全部
        "trials": args.trials,
        "workers": args.workers,
        "early_stop_on_contamination": not args.no_early_stop,
        "eval": controller.evaluator.describe(),
        "probe_judge": None,
    })

    results = controller.run(
        matrix,
        early_stop_on_contamination=not args.no_early_stop,
        max_workers=args.workers,
    )
    # Early-stop appends further skips to matrix.skipped during the run.
    run_skips = matrix.skipped[n_pre_skips:]
    if run_skips:
        print(f"\n运行中早停跳过 {len(run_skips)} 个 job:")
        for tid, m, reason in run_skips:
            print(f"  · skip {tid}/{m}: {reason}")

    contaminated = sum(1 for pr, _ in results if pr.contaminated)
    errors = sum(1 for pr, _ in results if pr.error is not None)
    valid = sum(1 for _, ev in results if ev.overall_valid and not ev.excluded)
    print(
        f"\n完成 {len(results)} 个 run: {valid} 个整体有效, {contaminated} 个判污染, "
        f"{errors} 个基础设施错误 (作废, 不计入 solve_rate)。"
    )
    print(f"产物已落盘到 {args.results_dir}/。运行 `leaderboard` 查看榜单。")
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


def cmd_export_web(args) -> int:
    from .webexport import build_site_data, write_data_js

    data = build_site_data(args.results_dir, args.tasks_dir, registry_path=args.registry)
    if not data["results"]:
        print(f"results 目录 {args.results_dir} 为空, 先跑 `run`。")
        return 1
    out = write_data_js(data, args.out)
    n = len(data["results"])
    print(f"已导出 {n} 个 run 的可视化数据 → {out}")
    print("本地预览: python -m http.server -d docs  (然后访问 http://localhost:8000)")
    return 0


def cmd_probe(args) -> int:
    from .specification import Job
    from .prover.runner import ProverRunner

    controller = _build_controller(args)
    task = controller.tasks[args.task]
    backend = controller.backend_for(args.model)
    runner = ProverRunner(backend, probe_judge=controller.probe_judge)
    job = Job(task_id=args.task, model=args.model, hint_level=0, trial=0)
    result = runner.run_probes(task, job)  # probe-only: 不进入 (昂贵的) 证明阶段
    print(f"探针结果 — {args.model} @ {args.task}:")
    for pr in result.probe_responses:
        flag = "❌ 泄露" if pr.leaked else "✅ 干净"
        hits = f" 命中:{pr.matched_indicators}" if pr.matched_indicators else ""
        print(f"  [{flag}] {pr.probe_id} ({pr.kind.value}){hits}")
    if result.error is not None and not result.contaminated:
        print(f"结论: 探针电池未跑完 (基础设施错误), 无法判定: {result.error}")
        return 1
    print("结论:", "污染除名" if result.contaminated else "探针洁净, 可计入正式分")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="breakthrough-eval", description=__doc__)
    p.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR)
    p.add_argument("--registry", default=DEFAULT_REGISTRY)
    p.add_argument("--results-dir", default=DEFAULT_RESULTS)
    p.add_argument("--eval-config", default=DEFAULT_EVAL_CONFIG)
    p.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="日志详细度: -v=INFO (每 job/阶段/API 调用进度), -vv=DEBUG",
    )
    p.add_argument("--log-level", default=None, help="显式日志级别 (DEBUG/INFO/WARNING/ERROR), 覆盖 -v")
    p.add_argument("--log-file", default=None, help="同时把 DEBUG 级日志写入该文件")
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
    r.add_argument(
        "--workers", type=int, default=4,
        help="prove+eval 并发度 (HTTP-bound; 1=顺序. 探针仍每 model+task 跑一次)",
    )
    r.add_argument("--no-early-stop", action="store_true")
    r.set_defaults(func=cmd_run)

    lb = sub.add_parser("leaderboard", help="渲染榜单")
    lb.set_defaults(func=cmd_leaderboard)

    ew = sub.add_parser("export-web", help="导出可视化站点数据 (docs/data.js, 供 GitHub Pages)")
    ew.add_argument("--out", default="docs/data.js", help="输出路径 (默认 docs/data.js)")
    ew.set_defaults(func=cmd_export_web)

    pr = sub.add_parser("probe", help="只跑污染探针")
    pr.add_argument("--task", required=True)
    pr.add_argument("--model", required=True)
    pr.set_defaults(func=cmd_probe)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(
        verbosity=getattr(args, "verbose", 0),
        log_file=getattr(args, "log_file", None),
        level=getattr(args, "log_level", None),
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
