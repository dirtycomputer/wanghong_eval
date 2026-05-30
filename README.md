# 通用「突破复现」评测系统 (breakthrough-eval)

把 Hassabis 的 **Einstein Test** 工程化为一个**可 scale up 的数学突破复现 benchmark**：
Controller 量产 `(突破问题, golden proof, rubric, hint 阶梯)`，PROVER 在**断网 + 时间冻结
arXiv** 下**先过探针再应考**，EVAL 用**强评委 + rubric** 给出 win rate，leaderboard 的真正
信息量落在**「需要多少 hint 才解得出」这条难度曲线**上。

第一个 task 是 **王虹测试**（截到 ~2025/01，看 harness 能否独立证明三维 Kakeya 猜想，
golden proof = Wang–Zahl, arXiv:2502.17655）。

> 本仓库是按 [`breakthrough_eval_system_plan.md`](breakthrough_eval_system_plan.md) 实现的
> **完整可运行框架**。所有模型/检索/评委后端都是**可插拔**的：默认用离线 mock 后端跑通全链路
> 并带测试；接真实 `codex exec` / vLLM / 强评委 API 只需实现同一接口。

---

## 安装

```bash
pip install -e .            # 依赖 pydantic>=2, PyYAML
pip install -e ".[dev]"     # 加 pytest
```

## 快速开始

```bash
# 1. 校验 TaskSpec 的红线 (model_cutoff <= retrieval_cutoff < breakthrough_date)
python -m breakthrough_eval validate

# 2. 看哪些 model 对该 task 合格 (cutoff < retrieval_cutoff)
python -m breakthrough_eval list-models --task kakeya_3d_wang_zahl

# 3. 跑通 Controller→PROVER(探针→证明)→EVAL→分数, 产物落盘 results/
#    --workers N 并发跑 prove+eval (HTTP-bound; 探针每 model+task 仍只跑一次)
python -m breakthrough_eval run --task kakeya_3d_wang_zahl --trials 5 --workers 4

# 4. 渲染主榜 + 每个 harness 的难度曲线
python -m breakthrough_eval leaderboard

# 5. 只跑污染探针 / 差分 sanity check
python -m breakthrough_eval probe --task kakeya_3d_wang_zahl --model leaky-eligible-by-date
python -m breakthrough_eval diff-check --task kakeya_3d_wang_zahl --model open-precutoff-weak
```

主榜示例（mock 后端）：

```
| Rank | Harness | Task | hint-AUC ↑ | cov-AUC | L0 pass@k | 最低可解 hint | rubric 峰值 | 探针洁净度 |
|------|---------|------|-----------|---------|-----------|--------------|------------|-----------|
| 1 | open-precutoff-strong+mock-deep   | 王虹测试 | 0.380 | 0.603 | 0% | L3 | 100% | ✅ clean |
| 2 | open-precutoff-mid+mock-vanilla   | 王虹测试 | 0.100 | 0.471 | 0% | L5 | 100% | ✅ clean |
| — | leaky-eligible-by-date+mock-deep  | 王虹测试 |   —   |   —   | —  | —  |  —   | ❌ 污染除名 |
```

L0 全 0、随 hint 升高才解出 —— 正是 Einstein-test 难度的预期表现（plan §10）。

---

## 架构与 plan 对应

```
Controller ──展开 job=(task×model×hint×trial)──▶ PROVER ──proof──▶ EVAL ──scores──▶ Leaderboard
   §6                                            §3              §4               §7
```

| 模块 | 文件 | plan |
|---|---|---|
| TaskSpec / 数据模型 | `models.py` | §2 |
| TaskSpec 加载与红线校验 | `taskspec.py` | §2, §8 |
| 时间冻结 arXiv 检索 | `arxiv_frozen.py` | §3.2, §8.2 |
| 污染探针 + 引用/越界审计 | `contamination.py` | §8.3, §8.4 |
| Hint 阶梯 + 提示拼装（rubric 不进提示） | `hints.py` | §5, §10.4 |
| PROVER 后端接口 / mock / codex exec | `prover/` | §3 |
| probe-then-prove 编排 | `prover/runner.py` | §3.3 |
| EVAL 评委接口 / mock / LLM / 多评委聚合 | `eval/` | §4 |
| Model Registry | `registry.py` | §6.2 |
| Controller / 调度 / 早停 / 差分检查 / 并发 | `controller.py` | §6, §8 |
| Leaderboard / hint-AUC / 难度曲线 | `leaderboard.py` | §7 |
| 产物落盘 | `storage.py` | §6.4 |
| CLI | `cli.py` | — |

### 污染防控的四道防线（make-or-break, §8）

1. **模型层** — `ModelEntry.eligible_for` / Controller 只放行 `cutoff < retrieval_cutoff` 的 model。
2. **检索层** — `ArxivFrozenSource` 在内部按 `submission_date <= cutoff` **硬过滤**，其余断网。
3. **探针层** — `ProverRunner` 先跑 `contamination_probes`，任一命中关键创新 → 判污染、跳过证明、除名。
4. **审计层** — 事后扫引用与 transcript：有 > cutoff 的引用或越界网络调用 → 该 run 作废。

> **差分 sanity check**（`diff-check`）：同一 model 跑冻结 vs 故意给突破后 arXiv 两遍。
> 若「给后置文献」突然让它解出而冻结版解不出，说明度量确实在测**独立性**而非检索能力。

### frontier delta（前沿增量）

Rubric 沿 frontier delta 切分：`frontier_delta=true` 是**突破本身的关键创新**（要测模型能否自己跨过），
`frontier_delta=false` 是 **T 之前已有的合法工具**（站在巨人肩膀上，本就允许）。整体「有效证明」的判据是
**所有 frontier-delta 关键创新都被独立触及并闭合**。

---

## 接入真实后端

### 最快路径：OpenRouter（已内置）

PROVER 和强评委都可直接走 OpenRouter（OpenAI 兼容），无需 codex / 自建 serving：

```bash
export OPENROUTER_KEY=sk-or-...          # 只从环境变量读, 不写进任何文件

python -m breakthrough_eval \
  --registry models_registry.openrouter.yaml \
  run --task kakeya_3d_wang_zahl --models gemma-4-31b --trials 1 \
  --judges "openrouter:deepseek/deepseek-v4-pro,mock:0.5"
```

- **PROVER**：`provider: openrouter` + `backend_kwargs.model`（见 `models_registry.openrouter.yaml`）。
  内置 `OpenRouterProverBackend` 跑一个带 `search_arxiv` 工具的 agent loop，工具只回时间冻结
  快照里的论文，每次调用都记进 transcript 供审计；探针阶段不给工具（要它的「无援」回答）。
- **评委**：`--judges openrouter:<model>`（可与 `mock` 混用做多评委一致性）。评委是现代强模型
  恰恰是想要的 —— EVAL 被允许知道答案（plan §4）。

> 关于王虹测试的有效性：`gemma-4-31b-it` 的官方 cutoff 是 **January 2025**（模型卡），
> **早于** Wang–Zahl 突破（2025-02-25），所以它训练里不会见过那篇证明 —— 实跑的污染探针
> 也确实全部 clean。它因此是一个**边界合法的 pre-breakthrough PROVER**。
> 注意 cutoff 是厂商宣称的软日期（plan §10.6 突破日期本身也可能因 talk/流出而模糊），
> 故 `cutoff_confidence: medium`，并以探针实测为最终判据。

### 其它后端

- **PROVER（codex exec, plan §3.2）**：把 registry 里的 `provider` 改成 `codex` / `local-vllm`，
  `backend_kwargs` 填 `model` / `model_provider`。`CodexProverBackend` 会按 plan §3.2 渲染
  `config.toml`（`web_search=disabled` + 时间冻结 arXiv MCP + `required=true`）并跑
  `codex exec --sandbox workspace-write --ask-for-approval never --ignore-user-config --json ...`。
  本环境无 `codex` 二进制时它会清晰报错而非裸跑。
- **真实冻结 arXiv**：实现 `ArxivFrozenSource` 的 `search`/`_corpus`（接你的 arXiv 快照 MCP），
  作为 `Controller(source_factory=...)` 注入。
- **强评委（LLM-as-judge, plan §4.2）**：给 `LLMJudge` 注入一个返回 JSON 的 `complete(system, user)`
  回调（Anthropic/OpenAI 均可），它会强制「逐条 rubric + 行号引用 + 不许笼统判断」。
  多评委一致性（Cohen's κ）、golden 锚定校准、人工复核队列在 `Evaluator` 里。

```python
from breakthrough_eval import Controller, Evaluator, ModelRegistry, load_all_tasks
from breakthrough_eval.eval import LLMJudge, MockJudge

evaluator = Evaluator([LLMJudge(complete=my_anthropic_call), MockJudge()])
controller = Controller(load_all_tasks("tasks"), ModelRegistry.load("models_registry.yaml"), evaluator)
matrix = controller.expand_job_matrix(["kakeya_3d_wang_zahl"], trials=5)
results = controller.run(matrix)
```

---

## 新增一个「XXX 测试」

TaskSpec 是纯数据，系统代码对所有 task 同构。新增 = 丢一份 `tasks/<id>.yaml` 并通过校验：

1. 填时间锚（必须满足 `allowed_model_cutoff_before <= retrieval_cutoff < breakthrough_date`）。
2. 写 **T 时刻表述**的题面（不得出现指向答案的方法名）。
3. 沿 frontier delta 切 `rubric`（标 `frontier_delta`，给机检 `indicators`）。
4. 写 `contamination_probes`（direct / indirect / completion）。
5. 写 `hint_ladder`（L0 冷启 → Lk 近满，标 `reveals_rubric_items`）。
6. `python -m breakthrough_eval validate tasks/<id>.yaml`。

---

## 测试

```bash
python -m pytest -q
```

覆盖：红线校验、冻结检索、探针/审计、probe-then-prove、多评委一致性与 golden 锚定、
job 矩阵与资格过滤、污染早停、leaderboard 聚合、差分 sanity check、codex 配置渲染。

## 已知局限（诚实地认, plan §10）

- 你几乎**无法证明**模型「绝对干净」，只能给污染上界 + 置信度；榜单把探针洁净度作为一等公民展示。
- 评委能力是天花板：前沿数学判定靠多评委 + golden 锚定 + （可选）Lean 形式化 + 人工复核兜底。
- 内置后端是 **mock**（离线、确定性、用于跑通与测试），不是真实模型能力的度量。
