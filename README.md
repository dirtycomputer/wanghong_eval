# 通用「突破复现」评测系统 (breakthrough-eval)

把 Hassabis 的 **Einstein Test** 工程化为一个**可 scale up 的数学突破复现 benchmark**：
Controller 量产 `(突破问题, golden proof, rubric, hint 阶梯)`，PROVER 在**断网 + 时间冻结
arXiv** 下**先过探针再应考**，EVAL 用**强评委 + rubric** 给出 win rate，leaderboard 的真正
信息量落在**「需要多少 hint 才解得出」这条难度曲线**上。

第一个 task 是 **王虹测试**（截到 ~2025/01，看 harness 能否独立证明三维 Kakeya 猜想，
golden proof = Wang–Zahl, arXiv:2502.17655）。

> 本仓库是按 [`breakthrough_eval_system_plan.md`](breakthrough_eval_system_plan.md) 实现的
> **完整可运行框架**。所有模型/检索/评委后端都是**可插拔**的：**默认即真实栈**
> （OpenRouter PROVER/评委 + 真实 arXiv 冻结检索 + opencode/Hermes harness）；
> mock 实现全部保留但只用于测试与离线演示（`--registry models_registry.mock.yaml`
> `--judges mock... --probe-judge none --arxiv-source mock`）。

---

## 安装

```bash
pip install -e .            # 依赖 pydantic>=2, PyYAML
pip install -e ".[dev]"     # 加 pytest
```

## 快速开始

**默认即真实栈**(需 `OPENROUTER_KEY` 环境变量,产生 API 费用):registry =
`models_registry.yaml`(gemma 三 harness),评委 = 真实四面板(qwen3.7-max /
minimax-m3 / kimi-k2.6 / deepseek-v4-pro,max_tokens 打满),探针语义评审 =
deepseek-v4-pro,冻结检索 = **真实 arXiv API**(服务端 `submittedDate` 过滤 +
客户端二次硬过滤,3s 礼貌限速)。

```bash
# 1. 校验 TaskSpec 红线 + task 单元测试 (golden 锚定 / 探针自测 / 语义校准)
python -m breakthrough_eval validate
python -m breakthrough_eval task-qa

# 2. 看哪些 model 对该 task 合格
python -m breakthrough_eval list-models --task kakeya_3d_wang_zahl

# 3. 真实评测: Controller→PROVER(探针→证明)→EVAL→分数, 产物落盘 results/
python -m breakthrough_eval run --task kakeya_3d_wang_zahl --trials 2 --workers 4

# 4. 渲染主榜 + 难度曲线 (+ ≥2 task 时的 BT 跨 task 总榜)
python -m breakthrough_eval leaderboard

# 5. 只跑污染探针 (关键词 + deepseek 语义双层) / 导出站点数据
python -m breakthrough_eval probe --task kakeya_3d_wang_zahl --model gemma-4-31b
python -m breakthrough_eval export-web
```

### 离线演示 / 测试(mock 栈)

mock 组件全部保留,但**只在显式要求时使用**(测试套件即此模式,零网络零费用):

```bash
python -m breakthrough_eval --registry models_registry.mock.yaml --arxiv-source mock \
  run --task kakeya_3d_wang_zahl --trials 5 --workers 4 \
  --judges "mock:0.4,mock:0.5,mock:0.7" --probe-judge none
```

### 调试 / 日志

系统默认安静；用 `-v` 打开进度日志（写到 **stderr**，结果仍在 stdout）：

```bash
python -m breakthrough_eval -v   run ...   # INFO: 每 job/阶段/API 调用进度+耗时
python -m breakthrough_eval -vv  run ...   # DEBUG: 请求细节、检索 query、审计细节
python -m breakthrough_eval --log-file run.log run ...   # 同时落 DEBUG 到文件
```

INFO 级会逐行打印：每次 OpenRouter 调用的 **延迟/provider/token 数**（定位"慢在哪"）、
探针逐条 clean/泄露、污染早停、证明阶段耗时/字数/检索次数、每个评委的 items/valid/κ/复核，
并行时带 `threadName` 区分并发 job。例如：

```
12:00:03 INFO [ThreadPoolExecutor-0_1] ...llm_client: chat ✓ model=google/gemma-4-31b-it 126.7s provider=Ambient tok(in/out)=665/1500 tool_calls=0
12:00:04 INFO [ThreadPoolExecutor-0_1] ...runner: job ...::L2::t0: 证明完成 130.2s, 2823 字, 检索 1 次, tok(in/out)=665/1500
12:00:31 INFO [ThreadPoolExecutor-0_1] ...evaluator: eval ...::L2::t0: 1/7 items, valid=False, κ=1.00, review=False
```

主榜示例（mock 后端，`--trials 5` 实跑输出）：

```
| Rank | Harness | Task | hint-AUC ↑ | cov-AUC | L0 pass@k | 最低可解 hint | rubric 峰值 | earned 峰值 | 探针洁净度 | 复核 | 错误 | 成本 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | open-precutoff-strong+mock-deep | kakeya_3d_wang_zahl | 0.340 | 0.586 | 0% | L3 | 100% | 100% | ✅ clean | 13 | 0 | 74.5 |
| 2 | open-precutoff-mid+mock-vanilla | kakeya_3d_wang_zahl | 0.140 | 0.477 | 0% | L4 | 100% | 20% | ✅ clean | 1 | 0 | 71.8 |
| 3 | open-precutoff-weak+mock-vanilla | kakeya_3d_wang_zahl | 0.100 | 0.471 | 0% | L5 | 100% | 0% | ✅ clean | 0 | 0 | 67.3 |
| — | leaky-eligible-by-date+mock-deep | kakeya_3d_wang_zahl | — | — | — | — | — | — | ❌ 污染除名 | — | 0 | 0.5 |
```

L0 全 0、随 hint 升高才解出 —— 正是 Einstein-test 难度的预期表现（plan §10）。

> **earned vs given 覆盖率**：hint 是累积注入的，L4 把引理陈述直接给了模型——echo 回来
> 也能"触及"对应 rubric 项。`earned` 只统计 **hint 尚未揭示**（`reveals_rubric_items`
> 之外）的项：上表三个 mock 模型 rubric 峰值都是 100%，earned 峰值 100%/20%/0% 才是
> 真实能力排序。某级全部项都已揭示时 earned 无定义（显示 `—`）。难度曲线同时画三条线
>（solve / coverage / earned）。

> **基础设施错误 ≠ 模型失败**：后端/网络异常（如 OpenRouter 重试耗尽）的 run 只记录 `error`
> 并作废 —— 不计入 solve_rate/AUC、不进探针洁净度分母，在主榜「错误」列单独计数。
> 探针阶段出错的 job 不进入证明阶段，半截探针电池也不会写入缓存（后续同组 job 会重跑探针）。
> 另外 `probe` 子命令只跑探针电池，不会触发昂贵的证明阶段。

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
2. **检索层** — 真实 `ArxivApiSource`(export.arxiv.org)服务端 `submittedDate` 过滤 + 客户端按 v1 提交日二次**硬过滤**;CLI harness 经 `arxiv-frozen-mcp`(`ARXIV_MCP_SOURCE=arxiv`)同源。`InMemoryArxivSource` 仅供测试(`--arxiv-source mock`)。
3. **探针层** — `ProverRunner` 先跑 `contamination_probes`，任一命中关键创新 → 判污染、跳过证明、除名。
   探针是除名级裁决，因此**确定性**：一律 `temperature=0`，且按**底层权重**缓存（同一模型挂多个
   harness 只探一次、判定一致；共享权重判污染时所有 harness 一起早停）。关键词匹配只是下限——
   配 `--probe-judge openrouter:<model>` 可加**语义级泄露评审**，抓换措辞的实质复现
  （关键词命中仍一票否决；语义评审解析失败 → 弃权不冤杀，留痕进产物）。
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
  run --task kakeya_3d_wang_zahl --models gemma-4-31b --trials 1 \
  --judges "openrouter:deepseek/deepseek-v4-pro,mock:0.5"
```

- **PROVER**：`provider: openrouter` + `backend_kwargs.model`（见 `models_registry.yaml`，即默认 registry）。
  内置 `OpenRouterProverBackend` 跑一个带 `search_arxiv` 工具的 agent loop，工具只回时间冻结
  快照里的论文，每次调用都记进 transcript 供审计；探针阶段不给工具（要它的「无援」回答）。
- **评委**：`--judges openrouter:<model>`（可与 `mock` 混用做多评委一致性）。评委是现代强模型
  恰恰是想要的 —— EVAL 被允许知道答案（plan §4）。

> 关于王虹测试的有效性：`gemma-4-31b-it` 的官方 cutoff 是 **January 2025**（模型卡），
> **早于** Wang–Zahl 突破（2025-02-25），所以它训练里不会见过那篇证明 —— 实跑的污染探针
> 也确实全部 clean。它因此是一个**边界合法的 pre-breakthrough PROVER**。
> 注意 cutoff 是厂商宣称的软日期（plan §10.6 突破日期本身也可能因 talk/流出而模糊），
> 故 `cutoff_confidence: medium`，并以探针实测为最终判据。

### CLI harness:opencode / Hermes Agent(已内置)

同一模型可以换 harness 对比(plan §7:harness = model + scaffold + toolset):

```bash
python -m breakthrough_eval run --task kakeya_3d_wang_zahl \
  --models gemma-4-31b,gemma-4-31b-opencode,gemma-4-31b-hermes --trials 2
```

- `provider: opencode` 走 `opencode run --format json`(`npm i -g opencode-ai`):
  每 job 独立工作目录 + XDG 隔离,`tools` 配置禁用 webfetch / websearch / bash /
  edit / write / patch(联网与改文件不进考场);
- `provider: hermes` 走 `hermes -z` headless(NousResearch hermes-agent 安装脚本):
  `platform_toolsets.cli: []` 清空内置工具面,且**每个 job 独立 HOME** ——
  Hermes 的跨会话记忆/自我成长在 benchmark 里属于 trial 间状态泄漏,必须 fresh;
- 两者唯一外部信息源都是随包安装的 **`arxiv-frozen-mcp`**(stdio MCP server,
  plan §3.2):`CUTOFF_DATE` 在服务端硬过滤,每次检索追加 JSONL transcript,
  跑完回读成 `ToolCall` 进 §8.4 审计 —— harness 自己说没联网不算数,transcript 才算;
- **探针阶段一律绕过 CLI 直连裸模型**(无工具、短回答):污染是模型属性,
  「无援作答」的语义不应被 harness 的系统提示/工具/记忆污染。

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
5. 写 `hint_ladder`（L0 冷启 → Lk 近满，标 `reveals_rubric_items` —— earned 覆盖率靠它折扣）。
6. 给 `golden_proof.proof_text` 填**证明概要/全文**（注入评委 prompt 做对齐——
   评委不应只凭"恰好背过这篇论文"判定；缺省 validate 会警告）。
7. `python -m breakthrough_eval validate tasks/<id>.yaml`。

---

## 可视化站点 (GitHub Pages)

`docs/` 是一个**零依赖纯静态**的评测过程可视化站点:概览指标卡、probe→prove→audit→eval
流水线统计、难度曲线 (solve rate / rubric 覆盖率 + 每 trial 散点)、job 矩阵;点开任一
cell 可看 **rubric × 评委判定矩阵**(逐项理由 + 行号引用 + 置信度)、证明全文 (§3.4 结构)、
探针问答、审计 transcript;底部任务卡展示完整 TaskSpec(时间锚、rubric、hint 阶梯、探针)。

```bash
python -m breakthrough_eval export-web   # 把 results/ + tasks/ 烘焙成 docs/data.js
python -m http.server -d docs            # 本地预览 → http://localhost:8000
```

**部署**:仓库 Settings → Pages → "Deploy from a branch" → 选分支 + `/docs` 目录,
站点即发布在 `https://<user>.github.io/wanghong_eval/`。之后每次跑完 `run`,重新
`export-web` 并提交 `docs/data.js` 即可更新。数据以 `window.BE_DATA` 内联进 JS,
`file://` 直开和 Pages 托管都不会有 CORS 问题;`docs/.nojekyll` 跳过 Jekyll 处理。

> 当前仓库内置的 `docs/data.js` 是一次真实正式跑的产物:gemma-4-31b @ OpenRouter
> `wandb/bf16`(无检索闭卷,max_tokens 打满)× L0–L5 × 2 trials,四强评委面板
> (qwen3.7-max / minimax-m3 / kimi-k2.6 / deepseek-v4-pro, 48/48 票有效)。

## 任务生产流水线(scale 的主维度)

每个 task 的合格窗口随时间关闭(新模型对旧突破永远不合格),benchmark 必须**持续摄入新突破**:

```bash
# 1. 起草: 强模型 (站在 EVAL 侧, 允许知道答案) + 王虹测试 few-shot → tasks/drafts/
python -m breakthrough_eval draft-task \
  --title "Klartag 测试 — 高维格球堆积密度的超线性改进" \
  --golden-arxiv 2504.05042 --breakthrough-date 2025-04-07 \
  --retrieval-cutoff 2025-03-31 --context-file notes.md

# 2. 人工终审: 修探针自我泄题/题面指向答案等, 移入 tasks/
# 3. 自动验收: 红线 + golden 锚定 + 探针自测 (+ 语义层校准)
python -m breakthrough_eval validate tasks/<id>.yaml
python -m breakthrough_eval task-qa --task <id> \
  --probe-judge openrouter:deepseek/deepseek-v4-pro
```

`task-qa` 是 task 的单元测试:**golden 锚定**(金标证明走评委管线必须满分)、
**探针可触发**(合成泄露样本必须报警,抓 threshold 配置死探针)、**探针不误杀**
(诚实回答 + 复述题面绝不触发,抓过宽指示词)、**语义层校准**(golden 概要喂探针
必须被语义评审抓住、诚实回答必须放行)、hint 阶梯完整性。

第二个任务 `klartag_lattice_sphere_packing`(2025-04 高维格球堆积 n² 突破)就是按这条
流水线生产的:deepseek 起草一次过红线;人工终审修了 4 处(P2 探针把答案递给模型、
题面预设 n² 目标值、泄露词过长、概要编号越界);`validate` 的泄露词扫描当场抓到草稿
framing notes 字面引用禁词;语义校准 3/3 探针命中 golden 概要、放行诚实回答。

## 跨 task 总榜(Bradley–Terry)

合格模型集随时间锚**不齐**(模型 A 合格 10 个任务、模型 B 只合格 3 个),直接平均
hint-AUC 不可比。`leaderboard` 在 ≥2 个 task 时自动追加 BT 总榜:只在双方**共同参赛**
的 task 上两两比较(字典序 hint-AUC > earned 峰值 > cov-AUC,平局各记 0.5),
MM 迭代拟合实力分;**比较图不连通时按连通分量分联赛,绝不跨组排名**——没有共同
任务的新旧模型之间不存在合法比较路径(持续上新任务正是为了架桥)。

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
