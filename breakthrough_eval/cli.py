"""Command-line interface for the breakthrough-eval system.

Subcommands:
  validate     校验一个/一批 TaskSpec
  task-qa      task 的单元测试: golden 锚定 + 探针自测 (生产流水线验收门)
  list-tasks   列出已加载的 task
  list-models  列出 registry 里的 model 及其对某 task 的资格
  run          展开 job 矩阵并跑通 Controller→PROVER→EVAL→分数, 落盘
  leaderboard  从 results 目录聚合并渲染榜单 + 难度曲线
  export-web   导出可视化站点数据 (docs/data.js, 供 GitHub Pages)
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
from .logging_util import setup_logging
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


def _parse_probe_judge(spec: str | None):
    """``--probe-judge openrouter:<model>`` → SemanticProbeJudge (None = 仅关键词)。"""
    if not spec:
        return None
    if spec.startswith("openrouter:"):
        from .eval.openrouter_judge import openrouter_probe_judge

        return openrouter_probe_judge(spec.split(":", 1)[1])
    raise SystemExit(f"未知 probe-judge spec: '{spec}' (用 openrouter:<model>)")


def _build_controller(args) -> Controller:
    tasks = load_all_tasks(args.tasks_dir)
    registry = ModelRegistry.load(args.registry)
    evaluator = Evaluator(_parse_judges(getattr(args, "judges", None)))
    store = ResultStore(args.results_dir)
    return Controller(
        tasks=tasks, registry=registry, evaluator=evaluator, store=store,
        probe_judge=_parse_probe_judge(getattr(args, "probe_judge", None)),
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
        "judges": [j.describe() for j in controller.evaluator.judges],
        "review_kappa_threshold": controller.evaluator.review_kappa_threshold,
        "probe_judge": controller.probe_judge.describe() if controller.probe_judge else None,
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
    # 跨 task 聚合: ≥2 个 task 且 ≥2 个 harness 时给 Bradley–Terry 总榜。
    task_ids = {r.task_id for r in board.rows}
    players = {r.harness for r in board.rows if not r.removed}
    if len(task_ids) >= 2 and len(players) >= 2:
        from .aggregate import AggregateBoard

        print("# 跨 task 总榜 (Bradley–Terry, 仅共同任务两两比较; 不连通分联赛)\n")
        print(AggregateBoard.build(board.rows).render_table())
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


def cmd_task_qa(args) -> int:
    from .taskqa import run_task_qa

    tasks = load_all_tasks(args.tasks_dir)
    chosen = [tasks[args.task]] if args.task else list(tasks.values())
    evaluator = Evaluator(_parse_judges(getattr(args, "judges", None)))
    probe_judge = _parse_probe_judge(getattr(args, "probe_judge", None))
    rc = 0
    for task in chosen:
        report = run_task_qa(task, evaluator, probe_judge=probe_judge)
        print(report.render())
        print()
        if not report.passed:
            rc = 1
    return rc


def cmd_draft_task(args) -> int:
    from .draft import draft_task, save_draft
    from .llm_client import OpenRouterClient

    example = Path(args.example).read_text(encoding="utf-8")
    notes = Path(args.context_file).read_text(encoding="utf-8") if args.context_file else ""
    client = OpenRouterClient(model=args.model, max_tokens=args.max_tokens, timeout=1800)
    text, issues = draft_task(
        title=args.title, golden_arxiv=args.golden_arxiv,
        breakthrough_date=args.breakthrough_date, retrieval_cutoff=args.retrieval_cutoff,
        context_notes=notes, example_yaml=example, client=client,
    )
    out_path = args.out or (
        f"tasks/drafts/draft_{args.golden_arxiv.replace('.', '_').replace('/', '_')}.yaml"
    )
    out = save_draft(text, out_path)
    print(f"草稿已写入 {out}")
    if issues:
        print("⚠️ 草稿未过红线校验 (保留待人工修订):")
        for i in issues:
            print(f"   {i}")
    else:
        print("✅ 草稿通过红线校验。下一步: 人工终审 → 移入 tasks/ → `task-qa` 验收。")
    return 0


def cmd_probe(args) -> int:
    from .models import Job
    from .prover.runner import ProverRunner

    controller = _build_controller(args)
    task = controller.tasks[args.task]
    backend = controller.backend_for(args.model)
    source = controller.source_factory(task.retrieval_cutoff)
    runner = ProverRunner(backend, source, probe_judge=controller.probe_judge)
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
    r.add_argument(
        "--judges",
        default=None,
        help="评委: mock / mock:0.5 / openrouter:<model> (逗号分隔; 默认 mock 三评委面板)",
    )
    r.add_argument(
        "--probe-judge",
        default=None,
        help="语义级探针泄露评审: openrouter:<model> (关键词命中仍一票否决; 默认仅关键词)",
    )
    r.set_defaults(func=cmd_run)

    lb = sub.add_parser("leaderboard", help="渲染榜单")
    lb.set_defaults(func=cmd_leaderboard)

    ew = sub.add_parser("export-web", help="导出可视化站点数据 (docs/data.js, 供 GitHub Pages)")
    ew.add_argument("--out", default="docs/data.js", help="输出路径 (默认 docs/data.js)")
    ew.set_defaults(func=cmd_export_web)

    dt = sub.add_parser("draft-task", help="用强模型起草 TaskSpec 草稿 (生产流水线·起草工位)")
    dt.add_argument("--title", required=True, help="突破标题")
    dt.add_argument("--golden-arxiv", required=True, help="golden 论文 arXiv id")
    dt.add_argument("--breakthrough-date", required=True)
    dt.add_argument("--retrieval-cutoff", required=True)
    dt.add_argument("--model", default="deepseek/deepseek-v4-pro", help="起草模型 (OpenRouter id)")
    dt.add_argument("--context-file", default=None, help="背景材料文件 (选题人笔记)")
    dt.add_argument("--example", default="tasks/kakeya_3d_wang_zahl.yaml", help="few-shot 示例 spec")
    dt.add_argument("--out", default=None, help="输出路径 (默认 tasks/drafts/<id>.yaml)")
    dt.add_argument("--max-tokens", type=int, default=16384)
    dt.set_defaults(func=cmd_draft_task)

    tq = sub.add_parser("task-qa", help="task 的单元测试: golden 锚定 + 探针自测 (上线验收门)")
    tq.add_argument("--task", default=None, help="task_id (默认全部)")
    tq.add_argument("--judges", default=None, help="同 run --judges (golden 锚定用)")
    tq.add_argument("--probe-judge", default=None, help="同 run --probe-judge (语义层校准用)")
    tq.set_defaults(func=cmd_task_qa)

    pr = sub.add_parser("probe", help="只跑污染探针")
    pr.add_argument("--task", required=True)
    pr.add_argument("--model", required=True)
    pr.add_argument("--probe-judge", default=None, help="同 run --probe-judge")
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
    setup_logging(
        verbosity=getattr(args, "verbose", 0),
        log_file=getattr(args, "log_file", None),
        level=getattr(args, "log_level", None),
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
