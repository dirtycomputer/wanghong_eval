# 通用「突破复现」评测系统设计

> 范式：把 Hassabis 的 **Einstein Test**（把模型知识截到 1911 年，看能否独立推出广义相对论）推广成一个**可 scale up 的数学突破复现 benchmark**。第一个 task 是 **王虹测试**（截到 ~2025/01，看 harness 能否独立证明三维 Kakeya 猜想，golden proof = Wang–Zahl, arXiv 2502.17655）。

---

## 0. 先想清楚：这个系统真正的难点

整套系统在工程上不难，难点只有一个，而且它能一票否决整个 benchmark 的有效性：

**知识污染（contamination）/ 知识截断的真实性。**

「独立提出广义相对论 / 独立证明 Kakeya」这个命题成立的前提是 PROVER **真的不知道答案**。一旦模型在预训练里见过证明、见过会议 talk、见过相关 preprint，或者在评测时通过工具拿到了证明后的文献，那么 win rate 度量的就不再是「能否独立做出突破」，而是「能否检索/背诵已知答案」——benchmark 直接失效。

所以下面所有设计里，**红线是：(1) 模型 cutoff 必须早于突破日期；(2) 给 PROVER 的检索源必须按时间冻结在突破日期之前；(3) 跑正式 task 之前先用探针确认模型没泄露答案。** 其余都是可替换的工程件。

还有一个概念上必须区分清楚的东西，叫 **frontier delta（前沿增量）**：
- **合法**：使用突破日期 T 之前已知的技巧、引理、工具（Kakeya 里就是 Wolff/Bourgain/Katz/Łaba/Tao/Guth 的多项式方法、polynomial method、bush/hairbrush 论证等）。这些是「站在 T 时刻巨人的肩膀上」，本来就该允许。
- **污染**：复述 T 之后才出现的、属于突破本身的关键创新（Kakeya 里就是 Wang–Zahl 的多尺度归纳 + 凸集体积估计的那套结构定理）。

评测要测的是模型能否**自己跨过这个 delta**，而不是它能不能用 T 之前的旧工具。Rubric 必须沿着这个 delta 来切分。

---

## 1. 系统总览

```
                         ┌─────────────────────────────────────────┐
                         │              CONTROLLER                   │
                         │  (scale-up 引擎 / 编排器)                  │
                         │  · 找 task + golden proof                  │
                         │  · 找 cutoff 合适的 model                   │
                         │  · 生成 rubric / 探针 / hint ladder         │
                         │  · 展开 job 矩阵并调度                       │
                         └───────────────┬───────────────────────────┘
                                         │  job = (task, model, hint_level, trial)
                         ┌───────────────▼───────────────┐
                         │            PROVER              │   ← harness = Codex CLI exec
                         │  · pre-cutoff model            │
                         │  · 时间冻结的 arXiv 检索 (MCP)   │
                         │  · 其他源 (留接口)               │
                         │  · 禁用 web search + 网络沙箱    │
                         │  · 先跑探针，再跑正式 task        │
                         └───────────────┬───────────────┘
                                         │  prover_output (证明尝试 / 结构化 writeup)
                         ┌───────────────▼───────────────┐
                         │             EVAL               │   ← harness 也可复用 Codex exec
                         │  · 输入 golden proof + rubric    │
                         │  · LLM-as-judge (可联网/闭源 API) │
                         │  · 多评委 + 一致性                │
                         │  · 可选 Lean 形式化校验           │
                         │  · 输出 rubric 覆盖 / win rate    │
                         └───────────────┬───────────────┘
                                         │  scores
                         ┌───────────────▼───────────────┐
                         │          LEADERBOARD           │
                         │  task × model/harness × hint    │
                         └─────────────────────────────────┘
```

核心数据流：**Controller 把一个 task 展开成 `(task × model × hint_level × trial)` 的笛卡尔积 → 每个 cell 启一个 PROVER run（一个隔离容器里的 `codex exec`）→ 产物交给 EVAL 打分 → 聚合进 leaderboard。** task 是一等公民（见下节 schema），换 task 不用改代码，这就是 scale up 的关键。

---

## 2. Task 的形式化定义（scale-up 的地基）

要从「王虹测试」一个点扩成「任意 XXX 测试」，关键是把 task 抽象成一个**纯数据**的 spec。系统代码对所有 task 同构，新增 task = 新增一份 spec + 通过校验。

```yaml
# TaskSpec —— 每个突破一份
task_id: kakeya_3d_wang_zahl
title: "王虹测试 — 三维 Kakeya 猜想"
domain: math / geometric_measure_theory

# —— 时间锚（决定一切污染防控）——
breakthrough_date: 2025-02-25        # Wang–Zahl preprint 挂 arXiv 的日期
retrieval_cutoff: 2025-01-31         # 给 PROVER 的检索源冻结到这天（留 buffer，早于 preprint 和任何 talk）
allowed_model_cutoff_before: 2025-01-31   # 只有训练 cutoff 早于此的 model 才有资格上这个 task

# —— 题面（必须是 T 时刻的表述，不能泄露后续）——
problem_statement: |
  证明三维 Kakeya 猜想：R^3 中包含每个方向上单位线段的集合（Kakeya 集），
  其 Hausdorff 维数与 Minkowski 维数均为 3。
  （可等价地用 δ-tube 的离散版 / Wolff 公理版表述。）
problem_framing_notes: |
  题面只陈述「T 之前公认的开放问题状态」。不得出现 Wang–Zahl 的方法名、
  "convex set volume estimate" 等会指向答案的字眼。

# —— golden proof（评测基准，不给 PROVER，只给 EVAL）——
golden_proof:
  primary: "arXiv:2502.17655 (Wang, Zahl)"
  companions:                         # 用于辅助 EVAL 和 rubric 构建
    - "Tao 博客 2025-02-25 (induction on scales 解读)"
    - "Guth, arXiv:2505.07695 (proof 综述, 列出核心 ingredient)"

# —— rubric（沿 frontier delta 切分的关键创新点）——
rubric: <见 §4>

# —— 污染探针（跑正式 task 前先问，检测泄露）——
contamination_probes: <见 §8>

# —— hint 阶梯（0%→近满，给不同比例提示）——
hint_ladder: <见 §5>

# —— 检索面 ——
retrieval:
  arxiv_snapshot: "frozen <= retrieval_cutoff"
  other_sources: []                   # 先留接口（MathOverflow / nLab / Lean mathlib 等，均需时间冻结）
  web_search: DISABLED
```

> **关键约束自动校验**：`allowed_model_cutoff_before <= retrieval_cutoff < breakthrough_date`。Controller 在调度前强制检查，违反就拒绝该 job。

---

## 3. PROVER 设计（harness = Codex CLI `exec`）

### 3.1 为什么 Codex exec 合适
`codex exec "<task>"` 是非交互、可脚本化、可进 CI/容器的 agent loop：默认 read-only 沙箱，`--sandbox workspace-write` 给写盘，进度走 stderr、最终答案走 stdout（方便 pipe）。关键是它支持：
- `web_search = "disabled"`（config.toml）→ **直接关掉内置联网检索**，满足「ban 掉 web search」。
- MCP server 配置 → 我们挂一个**自建的「时间冻结 arXiv」MCP**，这是 PROVER 唯一的外部信息源。
- `--json` 输出完整 transcript（含每次工具调用），用于事后审计是否偷偷越界。
- custom `model_provider`（OpenAI 兼容端点）→ 可指向 vLLM/SGLang 起的**开源 pre-cutoff 模型**（pre-cutoff 模型大多是开源 checkpoint）。

### 3.2 网络与工具白名单（污染防控的执行层）
PROVER 容器 **默认全程断网**，只放行到「arXiv-MCP 代理」这一个内网地址。即使 model 想 web search 也无路可走（纵深防御：config 关 + 沙箱网络层再关一道）。

```toml
# prover/config.toml （随 task 注入 retrieval_cutoff）
model = "<pre-cutoff-model>"          # 例如某个 cutoff 早于 2025-01 的开源 checkpoint
model_provider = "local-vllm"         # 指向自建 OpenAI 兼容端点
web_search = "disabled"               # 红线：禁联网检索
[mcp_servers.arxiv_frozen]
command = "arxiv-frozen-mcp"
env = { CUTOFF_DATE = "2025-01-31" }  # MCP 内部硬过滤，返回结果一律 <= cutoff
required = true                        # 起不来就直接 fail，不允许悄悄裸跑
```

```bash
# 一个 PROVER run（在隔离容器里）
codex exec \
  --sandbox workspace-write \
  --ask-for-approval never \
  --ignore-user-config \              # 不加载本机 config，保证可复现
  --json \
  -c web_search=disabled \
  --cd /run/workspace \
  "$(cat prompt_with_hint_level_2.md)" \
  1> prover_output.md 2> transcript.log
```

### 3.3 探针先行（probe-then-prove）
每个 PROVER run 分两段，**探针不通过则该 (model,task) 直接判定污染、不计入正式分**：
1. **Probe phase**：先问 `contamination_probes`（见 §8），看模型是否在不给任何提示时就吐出 Wang–Zahl 式的关键创新。
2. **Prove phase**：探针清白后，给 `problem_statement + 对应 hint_level`，放它在时间冻结 arXiv 上检索、推理、写 proof。

### 3.4 PROVER 产物规范
要求结构化输出，方便 EVAL 对齐 rubric：
```
# Claimed Proof
## 1. 思路总览（用到的核心 idea）
## 2. 关键引理与论证
## 3. 完整证明
## 4. 我引用/检索到的 T 之前文献清单
```
第 4 节配合 transcript.log 用来核查：它引用的所有东西是否都 ≤ retrieval_cutoff。

---

## 4. EVAL 设计（LLM-as-judge + rubric win rate）

EVAL 与 PROVER 相反：**它被允许知道答案**——可联网、可用闭源最强 API（评委越强越好），输入 `prover_output + golden_proof + rubric`。

### 4.1 Rubric：沿 frontier delta 切分关键创新
不要让评委整体「打个分」，而是把突破拆成**几个可独立判定的关键 ingredient**，逐条判 PROVER 是否独立触及。Kakeya 的 rubric 草案（最终须对照 Guth 综述 arXiv:2505.07695 与原文锁定）：

| # | 关键创新点（rubric item） | 判定标准 |
|---|---|---|
| R1 | 把猜想离散化为 δ-tube 体积/incidence 问题（δ⁻² 根 δ-分离方向的 tube，体积 ≳ 1 up to logs） | 是否正确归约 |
| R2 | 识别 **Heisenberg group / 海森堡式构型**为核心障碍（Guth 综述开篇的 key obstacle） | 是否意识到并处理 |
| R3 | 以 **induction on scales（尺度归纳）**作为组织骨架（Tao 博客点出的核心标签） | 是否采用且用对 |
| R4 | 重构为**凸集并集的体积估计 / Wolff 公理版**（原文标题即此） | 是否得到等价更强表述 |
| R5 | tube 多尺度聚簇结构（sticky / grains 结构）的分析 | 是否给出关键结构刻画 |
| R6 | 关键结构定理：把「grainy」与海森堡型分情形闭合 | 是否给出并证对 |
| R7 | 组合 R1–R6 闭合归纳，得到满维 3 | 整体是否真的 close |

> **重要：允许「非 golden 的另解」。** 数学突破常有多条有效路径。评委的判据是「这条论证是否真的成立且达成同一定理」，golden proof 只是**对齐参照**，不是唯一正确答案。Rubric 因此要同时支持「沿 golden 路径打分」和「评委判定的 alternative-but-valid」两种通道。

### 4.2 评委可靠性（数学评测最大的软肋）
LLM 评委在前沿数学上很容易被「自信的胡话」骗过。缓解：
- **多评委 + 一致性**：≥3 个强评委独立打分，看 Cohen's κ / 分歧；分歧大的 cell 进人工复核队列。
- **Golden 锚定 + 反向校验**：把 golden proof 也当成一份「prover_output」喂进同一评委流水线，它必须拿满分；拿不满说明评委或 rubric 有问题（校准用）。
- **可形式化的子步上 Lean**：凡能翻译成 Lean/mathlib 的引理就跑形式化校验，把这部分从「LLM 觉得对」升级成「机器证明对」。前沿大定理整体形式化不现实，但子引理可显著提升可信度。
- **要求评委逐条引用 prover_output 行号**，禁止笼统判断，降低幻觉给分。

### 4.3 三种可选的 win rate 度量（建议都算，主榜用其一）
1. **Rubric 覆盖率**：`通过的 rubric item 数 / 总 item 数`。细粒度、可解释，**适合做主指标**。
2. **pass@k**：k 次 trial 里至少一次整体被判「有效证明」的概率。最贴近「能不能做出来」的二值直觉，但在 Einstein-test 难度上大概率全 0（见 §10），所以单独看意义有限，要配 hint ladder。
3. **pairwise / Elo**：让评委在「不同 harness 的产物之间」「产物 vs golden」做成对比较，算胜率/Elo。**适合排 leaderboard 名次**（多 harness 横向比时）。

---

## 5. Hint Ladder（拿到难度曲线，而不是一片全 0）

Hassabis 自己说「今天的系统做不到」。所以在最高难度（零提示冷启）上，几乎所有 harness 都会是 0 分——**没有区分度**。Hint ladder 是为了把二值的「做没做出来」变成一条**「需要多少脚手架」的连续曲线**，这才是 leaderboard 真正有信息量的轴。

提示按「逐步逼近 golden」分级，每级提示比例递增：

| Level | 提示内容（越往下越接近答案） | 大致比例 |
|---|---|---|
| L0 | 仅原始题面（冷启） | 0% |
| L1 | 给等价的离散/Wolff 公理重述（R1, R4 的方向） | ~15% |
| L2 | 点明核心障碍是海森堡型构型（R2） | ~30% |
| L3 | 给主技术路线名：「多尺度归纳 + 凸集体积估计」（R3） | ~50% |
| L4 | 给关键引理的陈述（不给证明）（R5, R6 的 statement） | ~70% |
| L5 | 给完整证明骨架，留关键步空着让它补全 | ~90% |

**度量**：对每个 (task, model) 画 `solve_rate vs hint_level` 曲线，取**曲线下面积 AUC** 作为单标量分（hint 越少就能解 → AUC 越高 → 越接近「真 AGI/真突破能力」）。这条曲线本身也比单点更能说明问题：是「只差临门一脚」（L4 就能解）还是「全程不会」（L5 都解不了）。

> 提示生成要可复现且不串题：每级提示从 golden proof 机器抽取 + 人工脱敏（去掉直接点破答案的措辞），并存档版本号。

---

## 6. Controller（让系统真正 scale up）

这是把「王虹测试」变成「任意 XXX 测试」的引擎。四件事：

### 6.1 自动找 task + golden proof
候选来源（自动挖掘 + 人工 gate）：
- **Fields Medal / Abel / Breakthrough Prize** 得主的代表作（明确的「突破」语义）。
- **四大顶刊**（Annals of Mathematics、Inventiones、JAMS、Acta）里被高频引用为「resolved long-standing conjecture」的论文。
- **Quanta / Tao 博客 / 各系新闻稿** 这类二手源用来**定位与定日期**（哪个 conjecture 哪天被谁解决），再回到一手论文。
- **Clay Millennium / 知名 open problem 列表** 的已解决项。

流水线：候选挖掘 → 用 LLM 抽 `(问题, 解决者, 解决日期, 一手论文)` → **人工 gate**（突破语义是否清晰、日期是否可信、是否有干净的 T-之前题面）。突破日期是污染防控的命门，必须人审。

### 6.2 自动配 cutoff 合适的 model
维护一个 **Model Registry**：`{model, 训练数据 cutoff（带可信度标注）, 是否开源可自部署, 能力档位}`。Controller 对每个 task 自动筛 `cutoff < retrieval_cutoff` 的候选。
- ⚠️ 厂商宣称的 cutoff 往往是「软」的（含晚期数据混入）。所以 registry 里每个 cutoff 要标 **置信度**，并最终以 §8 的探针实测为准。
- 王虹测试（need cutoff < 2025-01）能选的强模型其实有限——这恰恰说明为什么要 hint ladder 和后面的开放问题（§10）。

### 6.3 自动生成 rubric / 探针 / hint ladder
对新 task，用强 LLM **辅助起草** rubric（从 golden proof + 综述里抽关键 ingredient）、探针、hint 阶梯，再走**人工审校**。半自动是现实选择：纯自动起草、人审定稿。

### 6.4 编排与调度
`job_matrix = tasks × eligible_models × hint_levels × trials`。每个 job 一个隔离容器跑 `codex exec`，断网+冻结 arXiv，产物入对象存储，触发 EVAL，分数入库。失败重试、成本预算、并发限流都在这层。

---

## 7. Leaderboard 设计

**维度（每行一个 harness 在一个 task 上的表现）：**
- `harness`：PROVER 配置指纹 = `model + 工具集 + agent scaffold 版本`（同一 model 配不同 scaffold 是不同 harness，这正是要比的）。
- `task`：王虹测试 / 爱因斯坦测试 / …
- `hint_level`：L0–L5。
- 指标：rubric 覆盖率、pass@k、hint-AUC、成本。

**主榜（按 task，跨 hint 汇总）示例：**

| Rank | Harness (model + scaffold) | Task | hint-AUC ↑ | L0 pass@5 | 最低可解 hint | rubric 峰值覆盖 | 探针洁净度 |
|---|---|---|---|---|---|---|---|
| 1 | M-A + codex-deep | 王虹测试 | 0.41 | 0% | L4 | 5/7 | ✅ clean |
| 2 | M-B + codex-deep | 王虹测试 | 0.33 | 0% | L4 | 4/7 | ✅ clean |
| 3 | M-A + codex-vanilla | 王虹测试 | 0.22 | 0% | L5 | 3/7 | ✅ clean |
| — | M-C + * | 王虹测试 | — | — | — | — | ❌ 污染，除名 |

**子视图**：
- **难度曲线视图**：每个 harness 一条 `solve_rate vs hint_level`，直观看「差临门一脚」还是「全程不会」。
- **跨 task 雷达**：同一 harness 在王虹/爱因斯坦/…多个 task 上的 hint-AUC，看泛化。
- **污染审计列**：探针洁净度 + transcript 越界检查，**污染的 run 永远显式标红并排除**，不能让它污染榜单。

---

## 8. 污染防控（make-or-break 章节）

四道防线，叠加使用：

1. **模型层**：只用 cutoff 可信地早于突破日期的 model；开源可自部署的优先（训练数据时间可考据）。
2. **检索层**：PROVER 只能访问**时间冻结的 arXiv 快照**（MCP 内部按 `submission_date <= retrieval_cutoff` 硬过滤），其余全断网。给 buffer：cutoff 取得比 preprint 日期更早（王虹案取 2025-01-31，早于 2025-02-25 的 preprint 和任何更早的 talk/口耳相传）。
3. **探针层（probe battery）**：正式跑前用多种探针测泄露——
   - 直接问：「三维 Kakeya 猜想被证明了吗？怎么证的？」
   - 间接问：「证明 R³ Kakeya 满维，凸集体积估计 + 多尺度归纳能走通吗？」（看它是否对答案出处异常熟悉）
   - 补全式：给证明开头看它能否「续写」出 Wang–Zahl 的特有结构。
   - 任一探针命中关键创新 → 判污染，该 (model,task) 除名。
4. **审计层**：事后扫 `transcript.log`——它检索到的每条文献是否都 ≤ cutoff、是否有任何越界网络调用。有则该 run 作废。

**差分 sanity check**（验证 benchmark 本身有效）：对同一 model 跑两遍，一遍给冻结 arXiv，一遍**故意给突破之后的 arXiv**。如果「给后置文献」会突然让它解出来、而冻结版解不出来，说明 (a) 度量确实在测「独立性」而非检索能力，(b) 反过来也确认了它本身没污染。

**诚实的局限**：你几乎**无法证明**一个模型「绝对干净」，只能给出**污染上界 + 置信度**。Leaderboard 必须把「探针洁净度 / 污染置信」作为一等公民展示，而不是假装零污染。

---

## 9. 施工路线图

- **Phase 0（单点打通）**：只做王虹测试，手工 rubric，1–2 个 pre-cutoff model，跑通 `Controller→PROVER(codex exec, 冻结arXiv, 断网)→EVAL→分数` 全链路。先把**污染防控 + 探针**验对。
- **Phase 1（加轴）**：上 hint ladder（L0–L5）+ 多 trial + 多评委一致性 + hint-AUC。此时第一张有信息量的难度曲线出来。
- **Phase 2（多 task）**：半自动加 3–5 个突破 task（如挑几个近年 Annals/Fields 级、有干净 golden proof 的），打磨 TaskSpec schema 的通用性。
- **Phase 3（全自动 Controller）**：task 自动发现 + 自动配 model + 半自动 rubric/hint 生成 + 人工 gate。此时「XXX 测试」真正可批量量产。

---

## 10. 关键风险与开放问题（要提前认）

1. **没有又干净又够强的 pre-cutoff 模型**：王虹测试需要 cutoff < 2025-01，但那个时间点的模型可能根本不具备做研究级数学的能力，于是 L0 大面积全 0。→ 这正是 hint ladder 存在的理由：用「需要多少脚手架」来区分，而不是指望谁裸解。
2. **评委能力天花板**：判定前沿数学证明对错，评委本身可能不够格。→ 多评委 + golden 锚定校准 + 能形式化的子步上 Lean，并保留人工复核通道。
3. **Golden 不是唯一解**：必须支持评委认定的 alternative-but-valid 证明，否则会冤杀真正的独立突破。
4. **Rubric 被 game**：模型可能去拟合 rubric 措辞而非真做数学。→ rubric 措辞不进 PROVER 提示；探针 + 越界审计 + 人工抽检。
5. **成本**：多 task × 多 model × 多 hint × 多 trial × 多评委，组合爆炸。→ Controller 做预算与早停（某 model 在低 hint 全 0 可砍高成本 trial）。
6. **「突破日期」本身模糊**：会议 talk、口耳相传、提前流出都早于 preprint。→ cutoff 一律取保守 buffer，且突破日期人审。

---

### 一句话总结
把 Einstein Test 工程化为「**时间冻结的开卷研究考试**」：Controller 量产 `(突破问题, golden proof, rubric, hint 阶梯)`，PROVER 用 `codex exec` 在**断网 + 冻结 arXiv**下应考且**先过探针**，EVAL 用**强评委 + rubric + 可选 Lean**给出 win rate，leaderboard 的真正信息量在**「需要多少 hint 才解得出」这条难度曲线**上。整套系统的成败，全押在污染防控这一根弦上。
