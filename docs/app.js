/* breakthrough-eval 可视化 — 纯静态零依赖。数据来自 data.js (window.BE_DATA)。 */
"use strict";

(function () {
  const D = window.BE_DATA;
  if (!D || !D.results || !D.results.length) {
    document.body.innerHTML = "<p style='padding:40px;font-size:16px'>没有数据:请先 " +
      "<code>python -m breakthrough_eval run ...</code> 然后 " +
      "<code>python -m breakthrough_eval export-web</code> 重新生成 data.js。</p>";
    return;
  }

  // ---------- helpers ----------
  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const pct = (x, d = 0) => (100 * x).toFixed(d) + "%";
  const fmt = (x, d = 3) => Number(x).toFixed(d);
  const judgeShort = (name) => String(name).split(":").pop().split("/").pop();

  // ---------- group results by (harness, task) ----------
  const groups = new Map(); // key -> {harness, task_id, items:[{prover, eval}]}
  for (const r of D.results) {
    const key = r.prover.harness + "@@" + r.prover.task_id;
    if (!groups.has(key)) groups.set(key, { harness: r.prover.harness, task_id: r.prover.task_id, items: [] });
    groups.get(key).items.push(r);
  }
  const rows = D.leaderboard.map((row) => ({
    row, key: row.harness + "@@" + row.task_id, group: groups.get(row.harness + "@@" + row.task_id),
  })).filter((x) => x.group);

  let cur = rows[0];
  let curJob = null;

  // ---------- header ----------
  function renderHeader() {
    const task = D.tasks[cur.row.task_id] || {};
    const judges = (cur.group.items.find((r) => r.eval && r.eval.judges.length) || { eval: { judges: [] } })
      .eval.judges.map((j) => judgeShort(j.judge_name));
    $("#hdr-meta").innerHTML =
      `任务 <b>${esc(task.title || cur.row.task_id)}</b> · harness <code>${esc(cur.row.harness)}</code>` +
      ` · ${cur.group.items.length} 个 run · 评委 ${judges.length ? esc(judges.join(" / ")) : "—"}`;
    $("#gen-at").textContent = "数据生成于 " + D.generated_at;
    if (rows.length > 1) {
      const sel = document.createElement("select");
      rows.forEach((r, i) => {
        const o = document.createElement("option");
        o.value = i; o.textContent = `${r.row.harness} @ ${r.row.task_id}`;
        if (r === cur) o.selected = true;
        sel.appendChild(o);
      });
      sel.onchange = () => { cur = rows[+sel.value]; curJob = null; renderAll(); };
      $("#overview-sub").replaceChildren(sel);
    }
  }

  // ---------- overview cards ----------
  function card(k, v, cls = "", unit = "") {
    return `<div class="card ${cls}"><div class="k">${esc(k)}</div>` +
      `<div class="v">${v}${unit ? `<span class="unit"> ${esc(unit)}</span>` : ""}</div></div>`;
  }
  function renderCards() {
    const r = cur.row;
    const usage = cur.group.items.reduce((a, x) => {
      a.tok += (x.prover.usage.input_tokens + x.prover.usage.output_tokens);
      a.sec += x.prover.usage.wall_seconds; return a;
    }, { tok: 0, sec: 0 });
    const lowest = r.lowest_solvable_hint == null ? "—" : "L" + r.lowest_solvable_hint;
    const clean = r.probe_cleanliness >= 1;
    $("#cards").innerHTML = [
      card("hint-AUC ↑ (解出)", fmt(r.hint_auc), r.hint_auc > 0 ? "good" : ""),
      card("cov-AUC (覆盖)", fmt(r.hint_auc_coverage)),
      card("L0 冷启 pass@k", r.l0_pass_at_k ? "✅" : "0%", r.l0_pass_at_k ? "good" : ""),
      card("最低可解 hint", lowest, lowest === "—" ? "" : "good"),
      card("rubric 峰值覆盖", pct(r.peak_rubric_coverage)),
      card("earned 峰值 (hint 未揭示部分)",
        r.peak_earned_coverage != null ? pct(r.peak_earned_coverage) : "—"),
      card("探针洁净度", r.removed ? "❌ 除名" : (clean ? "✅ clean" : pct(r.probe_cleanliness)),
        r.removed ? "bad" : (clean ? "good" : "warn")),
      card("人工复核", String(r.needs_review_count), r.needs_review_count ? "warn" : ""),
      card("基础设施错误", String(r.error_count), r.error_count ? "warn" : ""),
      card("PROVER 用量", (usage.tok / 1000).toFixed(1), "", "k tok / " + Math.round(usage.sec) + "s"),
    ].join("");
    $("#overview-sub").textContent ||= "";
  }

  // ---------- run config (PROVER × EVAL) ----------
  const REDLINE_BY_PROVIDER = {
    openrouter: "search_arxiv 原生工具 (tool-calling); 只路由支持 tools 的 provider; 无 web 工具",
    opencode: "arxiv-frozen MCP; 禁 webfetch/websearch/bash/edit/write/patch; 每 job 独立 cwd+XDG",
    hermes: "arxiv-frozen MCP; platform_toolsets 清空; 每 job 独立 HOME (无跨会话记忆)",
    codex: "config.toml: web_search=disabled + 冻结 arXiv MCP (required)",
    mock: "离线确定性模拟 (跑通链路用)",
  };

  function renderConfig() {
    const meta = D.run_meta || {};
    // 运行级参数
    const runChips = [];
    const chip = (k, v) => `<span class="badge"><b>${esc(k)}</b> ${esc(v)}</span>`;
    if (meta.created_at) runChips.push(chip("时间", meta.created_at));
    if (meta.trials != null) runChips.push(chip("trials", meta.trials));
    runChips.push(chip("hint 级", meta.hint_levels ? meta.hint_levels.join(",") : "全部 (L0–L5)"));
    if (meta.workers != null) runChips.push(chip("workers", meta.workers));
    if (meta.early_stop_on_contamination != null)
      runChips.push(chip("污染早停", meta.early_stop_on_contamination ? "开" : "关"));
    if (meta.registry) runChips.push(chip("registry", meta.registry));
    $("#config-run").innerHTML = runChips.join("") ||
      "<span class='muted small'>无 run_meta.json (旧版 run 产物); 以下从 registry/产物推断。</span>";

    // PROVER: 每个 harness 一行 (leaderboard 行 ↔ registry 条目)
    const reg = D.registry || {};
    let p = `<table class="vmatrix"><tr><th>registry 名</th><th>harness 指纹</th><th>provider</th>` +
      `<th>底层模型</th><th>cutoff (置信度)</th><th>关键参数</th><th>检索/红线</th></tr>`;
    for (const r of rows) {
      const sample = r.group.items[0];
      const mname = sample ? sample.prover.model : "?";
      const e = reg[mname];
      const kw = e ? e.backend_kwargs : {};
      const params = Object.entries(kw).filter(([k]) => k !== "model")
        .map(([k, v]) => `<span class="badge">${esc(k)}=${esc(v)}</span>`).join("") || "—";
      p += `<tr><td><code>${esc(mname)}</code></td><td><code>${esc(r.row.harness)}</code></td>` +
        `<td>${esc(e ? e.provider : "?")}</td><td><code>${esc(kw.model || "?")}</code></td>` +
        `<td>${e ? esc(e.cutoff_date) + ` <span class="muted">(${esc(e.cutoff_confidence)})</span>` : "—"}</td>` +
        `<td>${params}</td>` +
        `<td class="small">${esc(REDLINE_BY_PROVIDER[e ? e.provider : ""] || "—")}</td></tr>`;
    }
    p += "</table><p class='muted small' style='margin:6px 0 0'>三个 harness 的探针阶段均绕过 CLI " +
      "直连裸模型 (无援作答, 污染是模型属性); web search 一律禁用, 唯一外部信息源是时间冻结 arXiv。</p>";
    $("#config-prover").innerHTML = p;

    // EVAL: 评委面板
    let judges = meta.judges;
    if (!judges || !judges.length) {
      const sample = cur.group.items.find((x) => x.eval && x.eval.judges.length);
      judges = sample ? sample.eval.judges.map((j) => ({ name: j.judge_name })) : [];
    }
    if (judges.length) {
      let j = `<table class="vmatrix"><tr><th>评委</th><th>类型</th><th>模型</th>` +
        `<th>max_tokens</th><th>温度</th><th>其它</th></tr>`;
      for (const d of judges) {
        const extra = [];
        if (d.strictness != null) extra.push(`strictness=${d.strictness}`);
        if (d.timeout != null) extra.push(`timeout=${d.timeout}s`);
        if (d.max_parse_retries != null) extra.push(`解析重试=${d.max_parse_retries}`);
        j += `<tr><td><code>${esc(d.name)}</code></td><td>${esc(d.kind || "—")}</td>` +
          `<td><code>${esc(d.model || "—")}</code></td>` +
          `<td>${d.max_tokens != null ? esc(d.max_tokens) : "—"}</td>` +
          `<td>${d.temperature != null ? esc(d.temperature) : "—"}</td>` +
          `<td class="small">${esc(extra.join(" · ")) || "—"}</td></tr>`;
      }
      j += "</table>";
      $("#config-judges").innerHTML = j;
    } else {
      $("#config-judges").innerHTML = "<p class='muted small'>无评委配置信息。</p>";
    }

    // 语义级探针评审 (可选配置)
    if (meta.probe_judge) {
      const pj = meta.probe_judge;
      $("#config-judges").innerHTML +=
        `<p class="small" style="margin:8px 0 0">🕵️ 探针语义评审: <code>${esc(pj.name)}</code>` +
        (pj.model ? ` (model=${esc(pj.model)}, temp=${esc(pj.temperature)})` : "") +
        ` — 关键词命中仍一票否决, 语义评审抓换措辞的实质复现; 弃权不冤杀。</p>`;
    } else if (D.run_meta) {
      $("#config-judges").innerHTML +=
        `<p class="muted small" style="margin:8px 0 0">🕵️ 探针评审: 仅关键词匹配 (未配置语义评审 --probe-judge)。</p>`;
    }

    const kappa = meta.review_kappa_threshold != null ? meta.review_kappa_threshold : 0.6;
    $("#config-rules").innerHTML = [
      "逐项共识 = 评委严格多数决",
      `κ &lt; ${kappa} → 人工复核`,
      "整体/frontier-Δ 票分歧 → 复核",
      "评委解析失败 → 弃权 + 复核",
      "污染/审计失败 → 除名",
      "基础设施错误 → 作废 (不计 solve_rate)",
      "golden 锚定: 金标证明须拿满分",
    ].map((t) => `<span class="badge">${t}</span>`).join(" ");
  }

  // ---------- pipeline ----------
  function renderPipeline() {
    const items = cur.group.items;
    const probed = items.filter((x) => x.prover.probe_responses.length);
    const battery = probed.length ? probed[0].prover.probe_responses.length : 0;
    const leaked = probed.length ? probed[0].prover.probe_responses.filter((p) => p.leaked).length : 0;
    const proofs = items.filter((x) => x.prover.raw_output);
    const meanSec = proofs.length ? proofs.reduce((a, x) => a + x.prover.usage.wall_seconds, 0) / proofs.length : 0;
    const meanOut = proofs.length ? proofs.reduce((a, x) => a + x.prover.usage.output_tokens, 0) / proofs.length : 0;
    const searches = items.reduce((a, x) => a + x.prover.transcript.length, 0);
    const auditBad = items.filter((x) =>
      x.prover.audit.out_of_window_references.length || x.prover.audit.out_of_window_network.length).length;
    const votes = items.reduce((a, x) => a + (x.eval ? x.eval.judges.length : 0), 0);
    const abstain = items.reduce((a, x) => a + (x.eval ? x.eval.judges.filter((j) => j.parse_failed).length : 0), 0);
    const kappas = items.filter((x) => x.eval && x.eval.judges.length).map((x) => x.eval.agreement);
    const meanK = kappas.length ? kappas.reduce((a, b) => a + b, 0) / kappas.length : null;
    const review = items.filter((x) => x.eval && x.eval.needs_human_review).length;
    const errs = items.filter((x) => x.prover.error).length;
    const contaminated = items.filter((x) => x.prover.contaminated).length;

    $("#pipe").innerHTML = [
      `<div class="stage"><h3>🕵️ 探针 probe</h3><div class="big">${battery} 条 × 1 次</div>
       <ul><li>${leaked ? `❌ ${leaked} 条泄露 → 污染除名` : "✅ 全部 clean"}</li>
       <li>按 (model, task) 缓存复用</li><li>无工具、无提示作答</li></ul></div>`,
      `<div class="stage"><h3>📜 证明 prove</h3><div class="big">${items.length} 个 job</div>
       <ul><li>均值 ${meanSec.toFixed(1)}s / ${Math.round(meanOut)} tok 输出</li>
       <li>冻结 arXiv 检索 ${searches} 次</li>
       <li>${errs ? `⚠️ ${errs} 个基础设施错误 (作废)` : "0 错误"}${contaminated ? ` · ${contaminated} 判污染` : ""}</li></ul></div>`,
      `<div class="stage"><h3>🔍 审计 audit</h3><div class="big">${items.length - auditBad}/${items.length} 通过</div>
       <ul><li>引用均 ≤ retrieval_cutoff</li><li>无越界网络调用</li>
       <li>${auditBad ? `❌ ${auditBad} 个 run 作废` : "✅ 无违规"}</li></ul></div>`,
      `<div class="stage"><h3>⚖️ 评审 eval</h3><div class="big">${votes} 票</div>
       <ul><li>${abstain ? `⚠️ ${abstain} 票弃权 (解析失败)` : "0 弃权"}</li>
       <li>平均 κ ${meanK == null ? "—" : meanK.toFixed(2)}</li>
       <li>${review} 个 cell 进人工复核</li></ul></div>`,
    ].join("");
  }

  // ---------- difficulty curve (SVG) ----------
  function renderCurve() {
    const pts = [...cur.row.points].sort((a, b) => a.hint_level - b.hint_level);
    if (!pts.length) { $("#curve-chart").innerHTML = "<p class='muted'>无数据</p>"; return; }
    const maxL = Math.max(1, ...pts.map((p) => p.hint_level));
    const W = 760, H = 320, L = 50, R = 18, T = 18, B = 40;
    const x = (lv) => L + (lv / maxL) * (W - L - R);
    const y = (v) => T + (1 - v) * (H - T - B);
    let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">`;
    for (const g of [0, 0.25, 0.5, 0.75, 1]) {
      s += `<line x1="${L}" y1="${y(g)}" x2="${W - R}" y2="${y(g)}" stroke="#e3e7ee"/>` +
           `<text x="${L - 8}" y="${y(g) + 4}" text-anchor="end" font-size="11" fill="#6b7585">${g * 100}%</text>`;
    }
    for (const p of pts) {
      s += `<line x1="${x(p.hint_level)}" y1="${T}" x2="${x(p.hint_level)}" y2="${H - B}" stroke="#f0f2f6"/>` +
           `<text x="${x(p.hint_level)}" y="${H - B + 18}" text-anchor="middle" font-size="12" fill="#1c2330">L${p.hint_level}</text>`;
    }
    // per-trial coverage dots
    for (const it of cur.group.items) {
      if (!it.eval || it.eval.excluded || it.eval.errored || !it.eval.total_items) continue;
      const cov = it.eval.passed_items / it.eval.total_items;
      const jitter = (it.prover.trial - 0.5) * 10;
      s += `<circle cx="${x(it.prover.hint_level) + jitter}" cy="${y(cov)}" r="3.5" fill="#93b4f0" opacity="0.85">` +
           `<title>${esc(it.prover.job_id)} 覆盖 ${pct(cov)}</title></circle>`;
    }
    const line = (key, color, dash) =>
      `<polyline fill="none" stroke="${color}" stroke-width="2.5" ${dash ? `stroke-dasharray="6 4"` : ""} ` +
      `points="${pts.map((p) => `${x(p.hint_level)},${y(p[key])}`).join(" ")}"/>` +
      pts.map((p) => `<circle cx="${x(p.hint_level)}" cy="${y(p[key])}" r="4" fill="${color}">` +
        `<title>L${p.hint_level} ${key === "solve_rate" ? "solve" : "coverage"} ${pct(p[key])}` +
        ` (${p.n_trials} trials${p.n_excluded ? `, ${p.n_excluded} 排除` : ""}${p.n_errors ? `, ${p.n_errors} 错误` : ""})</title></circle>`).join("");
    s += line("mean_coverage", "#2563eb", true);
    // earned 覆盖率: 跳过 null (该级全部已被 hint 揭示, 无可挣项)。
    const epts = pts.filter((p) => p.mean_earned_coverage != null);
    if (epts.length) {
      s += `<polyline fill="none" stroke="#16a34a" stroke-width="2.5" stroke-dasharray="2 4" ` +
        `points="${epts.map((p) => `${x(p.hint_level)},${y(p.mean_earned_coverage)}`).join(" ")}"/>` +
        epts.map((p) => `<circle cx="${x(p.hint_level)}" cy="${y(p.mean_earned_coverage)}" r="4" fill="#16a34a">` +
          `<title>L${p.hint_level} earned ${pct(p.mean_earned_coverage)} (hint 未揭示部分)</title></circle>`).join("");
    }
    s += line("solve_rate", "#dc2626", false);
    s += `<g font-size="12"><rect x="${W - 238}" y="${T}" width="220" height="62" rx="8" fill="#fff" stroke="#e3e7ee"/>
      <line x1="${W - 226}" y1="${T + 14}" x2="${W - 196}" y2="${T + 14}" stroke="#dc2626" stroke-width="2.5"/>
      <text x="${W - 190}" y="${T + 18}">solve rate (整体有效率)</text>
      <line x1="${W - 226}" y1="${T + 32}" x2="${W - 196}" y2="${T + 32}" stroke="#2563eb" stroke-width="2.5" stroke-dasharray="6 4"/>
      <text x="${W - 190}" y="${T + 36}">rubric 覆盖率 (含 hint 白送)</text>
      <line x1="${W - 226}" y1="${T + 50}" x2="${W - 196}" y2="${T + 50}" stroke="#16a34a" stroke-width="2.5" stroke-dasharray="2 4"/>
      <text x="${W - 190}" y="${T + 54}">earned (自己挣到的)</text></g>`;
    s += "</svg>";
    $("#curve-chart").innerHTML = s;
  }

  // ---------- job matrix ----------
  function covColor(cov) { return `hsl(${120 * cov}, 65%, ${92 - 18 * cov}%)`; }
  function renderMatrix() {
    const items = cur.group.items;
    const levels = [...new Set(items.map((x) => x.prover.hint_level))].sort((a, b) => a - b);
    const trials = [...new Set(items.map((x) => x.prover.trial))].sort((a, b) => a - b);
    const byKey = new Map(items.map((x) => [x.prover.hint_level + ":" + x.prover.trial, x]));
    const task = D.tasks[cur.row.task_id];
    let html = "<table><tr><th>hint \\ trial</th>" + trials.map((t) => `<th>t${t}</th>`).join("") + "</tr>";
    for (const lv of levels) {
      const label = task ? (task.hint_ladder.find((h) => h.level === lv) || {}).label || "L" + lv : "L" + lv;
      html += `<tr><th>${esc(label)}</th>`;
      for (const t of trials) {
        const it = byKey.get(lv + ":" + t);
        if (!it) { html += "<td></td>"; continue; }
        const p = it.prover, e = it.eval;
        const flags = [];
        let bg = "#f4f5f7", covTxt = "—";
        if (p.contaminated) { flags.push("☣️ 污染"); bg = "#fdecec"; }
        else if (p.error) { flags.push("⚠️ 错误"); bg = "#fdf3e3"; }
        else if (p.audit.out_of_window_references.length || p.audit.out_of_window_network.length) {
          flags.push("🔍 审计✗"); bg = "#fdecec";
        } else if (e) {
          const cov = e.total_items ? e.passed_items / e.total_items : 0;
          covTxt = pct(cov); bg = covColor(cov);
          if (e.overall_valid) flags.push("✅ 有效");
          if (e.needs_human_review) flags.push("👀 复核");
          flags.push("κ " + e.agreement.toFixed(2));
        }
        html += `<td style="background:${bg}"><button class="cell" data-job="${esc(p.job_id)}">` +
          `<span class="cov">${covTxt}</span><span class="flags">${esc(flags.join(" · ")) || "&nbsp;"}</span></button></td>`;
      }
      html += "</tr>";
    }
    html += "</table>";
    $("#matrix-grid").innerHTML = html;
    $("#matrix-legend").innerHTML =
      `<span><span class="swatch" style="background:${covColor(0)}"></span>覆盖 0%</span>` +
      `<span><span class="swatch" style="background:${covColor(0.5)}"></span>50%</span>` +
      `<span><span class="swatch" style="background:${covColor(1)}"></span>100%</span>` +
      `<span><span class="swatch" style="background:#fdecec"></span>污染 / 审计作废</span>` +
      `<span><span class="swatch" style="background:#fdf3e3"></span>基础设施错误 (不计分)</span>`;
    for (const b of document.querySelectorAll(".cell")) {
      b.onclick = () => openJob(b.dataset.job);
    }
  }

  // ---------- detail ----------
  function findJob(jobId) { return cur.group.items.find((x) => x.prover.job_id === jobId); }

  function openJob(jobId, scroll = true) {
    const it = findJob(jobId);
    if (!it) return;
    curJob = jobId;
    try { history.replaceState(null, "", "#job=" + encodeURIComponent(jobId)); } catch (e) { /* file:// */ }
    for (const b of document.querySelectorAll(".cell")) b.classList.toggle("sel", b.dataset.job === jobId);
    $("#detail").classList.remove("hidden");
    $("#detail-title").textContent = "";
    renderDetailHead(it); renderEvalTab(it); renderProofTab(it); renderProbeTab(it); renderAuditTab(it);
    switchTab("eval");
    if (scroll) $("#detail-sec").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderDetailHead(it) {
    const p = it.prover, e = it.eval;
    const badges = [];
    if (p.contaminated) badges.push(`<span class="badge bad">☣️ 污染除名</span>`);
    if (p.error) badges.push(`<span class="badge warn">⚠️ ${esc(p.error)}</span>`);
    if (p.audit.out_of_window_references.length || p.audit.out_of_window_network.length)
      badges.push(`<span class="badge bad">🔍 审计未通过</span>`);
    if (e && !e.excluded && !e.errored) {
      badges.push(e.overall_valid
        ? `<span class="badge ok">✅ 整体有效 (多数决)</span>`
        : `<span class="badge bad">✗ 未构成有效证明</span>`);
      badges.push(`<span class="badge">rubric ${e.passed_items}/${e.total_items}</span>`);
      badges.push(`<span class="badge">κ ${e.agreement.toFixed(2)}</span>`);
      if (e.alternative_valid) badges.push(`<span class="badge delta">🧪 另解有效</span>`);
      if (e.needs_human_review) badges.push(`<span class="badge warn">👀 进人工复核</span>`);
    }
    badges.push(`<span class="badge">tok ${p.usage.input_tokens}/${p.usage.output_tokens}</span>`);
    badges.push(`<span class="badge">${p.usage.wall_seconds.toFixed(1)}s</span>`);
    $("#detail-head").innerHTML =
      `<div class="jid">${esc(p.job_id)}</div><div style="margin-top:6px">${badges.join(" ")}</div>`;
  }

  function switchTab(name) {
    for (const b of document.querySelectorAll("#detail-tabs button"))
      b.classList.toggle("on", b.dataset.tab === name);
    for (const pane of ["eval", "proof", "probe", "audit"])
      $("#tab-" + pane).classList.toggle("hidden", pane !== name);
  }
  for (const b of document.querySelectorAll("#detail-tabs button")) b.onclick = () => switchTab(b.dataset.tab);

  function renderEvalTab(it) {
    const e = it.eval, task = D.tasks[cur.row.task_id];
    if (!e) { $("#tab-eval").innerHTML = "<p class='muted'>无评审结果。</p>"; return; }
    if (e.excluded) {
      $("#tab-eval").innerHTML = "<p class='muted'>该 run 因污染/审计失败被<b>排除计分</b> (plan §7/§8),评委未介入。</p>"; return;
    }
    if (e.errored) {
      $("#tab-eval").innerHTML = "<p class='muted'>该 run 因<b>基础设施错误作废</b> (≠ 模型没解出来),不送评委、不计入 solve_rate。</p>"; return;
    }
    const judges = e.judges;
    const items = task ? task.rubric : Object.keys(e.item_consensus).map((id) => ({ id, title: "", frontier_delta: false }));
    const vmap = judges.map((j) => Object.fromEntries(j.item_verdicts.map((v) => [v.item_id, v])));

    let html = `<div class="kv">` + judges.map((j) =>
      `<span class="badge ${j.parse_failed ? "warn" : (j.overall_valid ? "ok" : "bad")}">` +
      `${esc(judgeShort(j.judge_name))}: ${j.parse_failed ? "弃权" : (j.overall_valid ? "整体✓" : "整体✗")}</span>`).join("") + `</div>`;

    html += `<table class="vmatrix"><tr><th class="item-t">rubric item</th>` +
      judges.map((j) => `<th>${esc(judgeShort(j.judge_name))}</th>`).join("") + `<th>共识</th></tr>`;
    const revealed = new Set(e.revealed_items || []);
    for (const item of items) {
      html += `<tr><td class="item-t"><b>${esc(item.id)}</b> ${esc(item.title)} ` +
        (item.frontier_delta ? `<span class="badge delta">frontier Δ</span>` : "") +
        (revealed.has(item.id) ? `<span class="badge warn" title="该级 hint 已揭示此项方向: 不计入 earned">hint 已揭示</span>` : "") + `</td>`;
      for (let k = 0; k < judges.length; k++) {
        const v = vmap[k][item.id];
        if (judges[k].parse_failed) html += `<td class="c muted">—</td>`;
        else if (!v) html += `<td class="c muted" title="该评委未对此项给出判定">·</td>`;
        else html += `<td class="c ${v.passed ? "pass" : "fail"}" title="${esc(v.justification)}` +
          `${v.cited_lines.length ? " (行 " + v.cited_lines.join(",") + ")" : ""}">${v.passed ? "✓" : "✗"}</td>`;
      }
      const c = e.item_consensus[item.id];
      html += `<td class="c ${c ? "pass" : "fail"}"><b>${c ? "✓" : "✗"}</b></td></tr>`;
    }
    html += `<tr class="consensus-row"><td>整体有效 (所有 frontier-Δ 闭合)</td>` +
      judges.map((j) => `<td class="c ${j.parse_failed ? "muted" : (j.overall_valid ? "pass" : "fail")}">` +
        `${j.parse_failed ? "—" : (j.overall_valid ? "✓" : "✗")}</td>`).join("") +
      `<td class="c ${e.overall_valid ? "pass" : "fail"}"><b>${e.overall_valid ? "✓" : "✗"}</b></td></tr></table>`;

    html += `<h4 style="margin:16px 0 4px">逐项判定理由 (行号引用, 禁止笼统判断 — plan §4.2)</h4>`;
    for (const item of items) {
      html += `<details class="j"><summary><b>${esc(item.id)}</b> ${esc(item.title)} — 共识 ` +
        `${e.item_consensus[item.id] ? "✓ 触及" : "✗ 未触及"}</summary><div class="body">` +
        `<div class="muted small">判定标准: ${esc(item.criterion || "—")}</div>`;
      for (let k = 0; k < judges.length; k++) {
        const j = judges[k], v = vmap[k][item.id];
        html += `<div class="jrow"><span class="who">${esc(judgeShort(j.judge_name))}</span>: ` +
          (j.parse_failed ? `<span class="muted">弃权</span>` :
            v ? `${v.passed ? "✓" : "✗"} ${esc(v.justification)} ` +
                `<span class="cited">${v.cited_lines.length ? "引用行 " + v.cited_lines.join(", ") : "无行号"}` +
                ` · 置信 ${v.confidence}</span>`
              : `<span class="muted">未给出该项判定</span>`) + `</div>`;
      }
      html += `</div></details>`;
    }

    html += `<details class="j"><summary>评委备注 notes</summary><div class="body">` +
      judges.map((j) => `<div class="jrow"><span class="who">${esc(judgeShort(j.judge_name))}</span>: ` +
        `${esc(j.notes || "—")}</div>`).join("") + `</div></details>`;
    $("#tab-eval").innerHTML = html;
  }

  function renderProofTab(it) {
    const p = it.prover, so = p.structured_output;
    if (!so && !p.raw_output) {
      $("#tab-proof").innerHTML = `<p class='muted'>无证明产物${p.error ? " (基础设施错误: " + esc(p.error) + ")" : ""}。</p>`;
      return;
    }
    const sec = (title, body) => body && body.trim()
      ? `<h4>${esc(title)}</h4><pre class="prose">${esc(body)}</pre>` : "";
    let html = `<div class="kv"><span class="badge">输入 ${p.usage.input_tokens} tok</span>` +
      `<span class="badge">输出 ${p.usage.output_tokens} tok</span>` +
      `<span class="badge">${p.usage.wall_seconds.toFixed(1)}s</span>` +
      `<span class="badge">检索 ${p.transcript.length} 次</span></div>`;
    if (so) {
      html += sec("1 · 思路总览", so.idea_overview) + sec("2 · 关键引理与论证", so.key_lemmas) +
        sec("3 · 完整证明", so.full_proof);
      html += `<h4>4 · 引用文献</h4>` + (so.cited_references.length
        ? `<ul>${so.cited_references.map((r) => `<li><code>${esc(r)}</code></li>`).join("")}</ul>`
        : `<p class="muted">（未列出文献）</p>`);
      if (p.raw_output) html += `<details class="j"><summary>原始输出 raw_output</summary>` +
        `<div class="body"><pre class="prose">${esc(p.raw_output)}</pre></div></details>`;
    } else {
      html += sec("原始输出", p.raw_output);
    }
    $("#tab-proof").innerHTML = html;
  }

  function renderProbeTab(it) {
    const p = it.prover, task = D.tasks[cur.row.task_id];
    if (!p.probe_responses.length) {
      $("#tab-probe").innerHTML = "<p class='muted'>该 job 复用了同 (model, task) 已缓存的探针结果 (plan §8.3),或无探针。</p>";
      return;
    }
    let html = `<p class="muted small">探针在证明<b>之前</b>、无工具无提示下作答;任一命中关键创新 → 判污染、跳过证明 (plan §3.3/§8.3)。</p>`;
    for (const pr of p.probe_responses) {
      const spec = task ? task.contamination_probes.find((q) => q.id === pr.probe_id) : null;
      html += `<div class="probe-card"><h4>${esc(pr.probe_id)} <span class="badge">${esc(pr.kind)}</span> ` +
        (pr.leaked ? `<span class="badge bad">❌ 泄露</span>` : `<span class="badge ok">✅ clean</span>`) + `</h4>` +
        (spec ? `<div class="prompt">Q: ${esc(spec.prompt)}</div>` : "") +
        `<pre class="prose">${esc(pr.response_text)}</pre>` +
        (pr.matched_indicators.length
          ? `<div>命中: ${pr.matched_indicators.map((m) => `<span class="badge bad">${esc(m)}</span>`).join("")}</div>` : "") +
        `</div>`;
    }
    $("#tab-probe").innerHTML = html;
  }

  function renderAuditTab(it) {
    const p = it.prover, a = p.audit;
    const ok = !a.out_of_window_references.length && !a.out_of_window_network.length;
    let html = `<div class="kv">` + (ok
      ? `<span class="badge ok">✅ 审计通过:引用均 ≤ cutoff,无越界网络</span>`
      : `<span class="badge bad">❌ 审计未通过 → run 作废 (plan §8.4)</span>`) + `</div>`;
    if (a.out_of_window_references.length)
      html += `<h4>越界引用</h4><ul>${a.out_of_window_references.map((r) => `<li><code>${esc(r)}</code></li>`).join("")}</ul>`;
    if (a.out_of_window_network.length)
      html += `<h4>越界网络调用</h4><ul>${a.out_of_window_network.map((r) => `<li><code>${esc(r)}</code></li>`).join("")}</ul>`;
    html += `<h4>检索 transcript (${p.transcript.length} 次)</h4>`;
    html += p.transcript.length
      ? `<table class="vmatrix"><tr><th>工具</th><th>query</th><th>host</th><th>返回论文日期</th></tr>` +
        p.transcript.map((t) => `<tr><td>${esc(t.tool)}</td><td><code>${esc(t.query)}</code></td>` +
          `<td><code>${esc(t.host)}</code></td><td>${esc(t.returned_dates.join(", "))}</td></tr>`).join("") + `</table>`
      : `<p class="muted">本 run 没有任何检索调用${p.transcript.length === 0 ? " (如 endpoint 不支持工具的闭卷模式)" : ""}。</p>`;
    $("#tab-audit").innerHTML = html;
  }

  // ---------- task card ----------
  function renderTask() {
    const t = D.tasks[cur.row.task_id];
    if (!t) { $("#task-card").innerHTML = "<p class='muted'>无任务数据</p>"; return; }
    let html = `<h3 style="margin:0 0 4px">${esc(t.title)} <span class="badge">${esc(t.domain)}</span></h3>`;
    html += `<div class="timeline">
      <span class="tl-node">model cutoff ≤ <b>${esc(t.allowed_model_cutoff_before)}</b></span><span class="tl-arrow">≤</span>
      <span class="tl-node">检索冻结 <b>${esc(t.retrieval_cutoff)}</b></span><span class="tl-arrow">&lt;</span>
      <span class="tl-node">💥 突破 <b>${esc(t.breakthrough_date)}</b></span></div>`;
    html += `<h4>题面 (T 时刻表述)</h4><pre class="prose">${esc(t.problem_statement)}</pre>`;
    html += `<p class="muted small">golden proof (只给评委): <code>${esc(t.golden_proof.primary)}</code>` +
      (t.golden_proof.companions.length ? ` · 辅助: ${t.golden_proof.companions.map(esc).join("; ")}` : "") + `</p>`;
    html += `<h4>Rubric (${t.rubric.length} 项, 沿 frontier delta 切分)</h4>`;
    for (const r of t.rubric) {
      html += `<div class="rubric-item"><b>${esc(r.id)}</b> ${esc(r.title)} ` +
        (r.frontier_delta ? `<span class="badge delta">frontier Δ (突破关键创新)</span>`
                          : `<span class="badge">pre-T 合法工具</span>`) +
        `<div class="crit">${esc(r.criterion)}</div></div>`;
    }
    html += `<h4 style="margin-top:16px">Hint 阶梯 (${t.hint_ladder.length} 级, 累积给出)</h4>`;
    for (const h of t.hint_ladder) {
      html += `<details class="j"><summary><b>${esc(h.label)}</b> · ~${Math.round(h.ratio * 100)}%` +
        (h.reveals_rubric_items.length ? ` · 揭示 ${h.reveals_rubric_items.map(esc).join(", ")}` : "") +
        `</summary><div class="body">${h.content ? `<pre class="prose">${esc(h.content)}</pre>`
          : `<p class="muted">冷启,无额外提示。</p>`}</div></details>`;
    }
    html += `<h4 style="margin-top:16px">污染探针 (${t.contamination_probes.length} 条)</h4>`;
    for (const q of t.contamination_probes) {
      html += `<details class="j"><summary><b>${esc(q.id)}</b> <span class="badge">${esc(q.kind)}</span></summary>` +
        `<div class="body"><pre class="prose">${esc(q.prompt)}</pre>` +
        `<div>泄露判定词 (阈值 ${q.threshold}): ${q.leak_indicators.map((m) => `<span class="badge">${esc(m)}</span>`).join("")}</div></div></details>`;
    }
    $("#task-card").innerHTML = html;
  }

  // ---------- boot ----------
  function renderAll() {
    renderHeader(); renderCards(); renderConfig(); renderPipeline(); renderCurve(); renderMatrix(); renderTask();
    $("#detail").classList.add("hidden");
    $("#detail-title").textContent = "← 在上方矩阵选择一个 cell";
    const m = location.hash.match(/^#job=(.+)$/);
    if (m) {
      const jid = decodeURIComponent(m[1]);
      if (findJob(jid)) openJob(jid, false);
    }
  }
  renderAll();
})();
