/* TraceJudge-Hy3 recording demo page controller.
 *
 * All displayed run data comes from the local server's /api/run endpoints,
 * which execute the real project pipeline.  Nothing here fabricates results:
 * the reveal pacing below is only presentation timing applied to one
 * completed, real run.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const RECORDING = params.get("recording") === "1";
  if (RECORDING) document.body.classList.add("recording");

  const REVEAL_DELAY_MS = 1300;
  const POLL_MS = 600;

  let currentMode = "fixture";
  let running = false;
  let currentRunId = null;
  let showcaseData = null;
  let regressionData = null;
  let runData = null;
  let overviewReady = false;
  let runFailed = false;
  let sceneIndex = 0;
  let tourIndex = -1;
  let tourReturn = null;
  let tourNavigation = 0;
  let currentValidationReady = false;
  const SCENES = [
    ["card-problem", "题目", "从一条明确需求开始", "展示输入范围与预期行为，再点击开始评估。"],
    ["card-solution", "解答", "解题说明与代码，一起接受检查", "查看本次解答的需求理解、实现步骤与代码；这里展示的是模型输出的说明。"],
    ["card-execution", "测试", "先看代码实际做了什么", "比较输入、预期结果与实际输出。测试结论只覆盖本次执行的用例。"],
    ["card-alignment", "对齐", "检查说明是否与实现一致", "结构特征是辅助证据；是否违反要求，仍需结合具体需求与执行结果。"],
    ["card-assessment", "定位", "把判断落到步骤与依据", "分别查看功能结论、过程结论与首错位置。高亮范围采用评估器原始输出，不额外推断精确行。"],
    ["card-certificate", "证书", "让已确认的失败可以复查", "查看实际反例、证书等级与自动重放结果。未发现错误或证据不足时，如实展示。"],
    ["card-contrast", "对照", "测试通过，为什么还要检查过程？", "切换到另一条已冻结的公开构造案例：代码未变，说明却与实现矛盾。"],
    ["overview", "验证", "用冻结研究与最新补充验证说明方法的表现", "历史 57 条冻结研究与 2026-09-10 最新补充验证分列展示，与刚才的单次 Demo 分别统计；不同实验不合并排名。"],
    ["card-costs", "成本", "效果与开销，放在一起看", "上方是本次样本的 Solver 与 Judge 开销；下方是冻结研究集的方法对照，保留来源和统计口径。"],
  ];

  function sceneAvailable(index) {
    if (index < 0 || index >= SCENES.length) return false;
    if (running) return index === 0;
    if (index === 0) return true;
    if (index === 6) return Boolean(showcaseData);
    if (index === 7) return overviewReady;
    if (index === 8) return true;
    return Boolean(runData) || (index === 1 && runFailed);
  }

  function updatePresenter() {
    if (!RECORDING) return;
    const [id, , title, note] = SCENES[sceneIndex];
    document.body.classList.toggle("recording-input", sceneIndex === 0);
    $("control-panel").querySelector("h3").textContent = sceneIndex === 0 ? "运行模式" : "本次运行产物";
    for (const [cardId] of SCENES) {
      $(cardId).classList.toggle("recording-active", cardId === id);
    }
    $("scene-count").textContent = `${String(sceneIndex + 1).padStart(2, "0")} / ${String(SCENES.length).padStart(2, "0")} · 展示步骤`;
    $("scene-title").textContent = title;
    $("scene-note").textContent = note;
    $("scene-source").textContent = sceneIndex === 8
      ? "本次样本明细 ＋ 已发布研究方法对照 · 两种统计范围"
      : sceneIndex === 6
      ? "已冻结公开构造案例 · 非本次生成／评审结果"
      : sceneIndex === 7
        ? "历史 57 条冻结研究 ＋ 2026-09-10 最新补充验证 · 均非本次运行统计"
        : running
          ? "本次流水线执行中 · 完成后可逐幕查看"
          : runData
            ? `${runData.mode === "fixture" ? "公开 Fixture · Mock 解答与评审" : "真实 Hy3 解答与评审"} · Run ${currentRunId || "—"}`
            : runFailed ? "本次运行失败 · 未替换为示例结果" : "预置公开题目 · 等待开始";
    $("scene-prev").disabled = !sceneAvailable(sceneIndex - 1) || sceneIndex === 0;
    $("scene-next").disabled = sceneIndex === SCENES.length - 1 || !sceneAvailable(sceneIndex + 1);
    document.querySelectorAll(".scene-tab").forEach((button, index) => {
      button.disabled = !sceneAvailable(index);
      button.setAttribute("aria-current", index === sceneIndex ? "step" : "false");
    });
  }

  function selectScene(index) {
    if (index < 0 || index >= SCENES.length || !sceneAvailable(index)) return;
    sceneIndex = index;
    updatePresenter();
  }

  function initPresenter() {
    if (!RECORDING) return;
    $("presenter").hidden = false;
    $("card-contrast").hidden = false;
    $("flow").appendChild($("overview"));
    SCENES.forEach(([, label], index) => {
      const button = el("button", "scene-tab", `${index + 1} ${label}`);
      button.addEventListener("click", () => selectScene(index));
      $("scene-tabs").appendChild(button);
    });
    $("scene-prev").addEventListener("click", () => selectScene(sceneIndex - 1));
    $("scene-next").addEventListener("click", () => selectScene(sceneIndex + 1));
    document.addEventListener("keydown", (event) => {
      if (tourActive()) return; // the guided tour owns arrow/escape keys while open
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
          /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        selectScene(sceneIndex + (event.key === "ArrowRight" ? 1 : -1));
      }
    });
    updatePresenter();
  }

  // ---------------------------------------------------------------- helpers

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function kv(dl, key, value, mono) {
    dl.appendChild(el("dt", null, key));
    const dd = el("dd", mono ? "mono" : null, value === null || value === undefined ? "—" : value);
    dl.appendChild(dd);
  }

  function setStage(n, state) {
    // state: "" | "running" | "done" | "failed"
    document.querySelectorAll(`#stages li[data-stage="${n}"]`).forEach((li) => {
      li.classList.remove("running", "done", "failed");
      if (state) li.classList.add(state);
    });
  }

  function setCaption(n, state) {
    // state: "" | "active" | "done"
    document.querySelectorAll(`.caption[data-stage="${n}"]`).forEach((c) => {
      c.classList.remove("active", "done");
      if (state) c.classList.add(state);
    });
  }

  function resetCaptions() {
    document.querySelectorAll(".caption").forEach((c) => c.classList.remove("active", "done"));
  }

  function revealCard(id) {
    const card = $(id);
    card.classList.remove("pending");
    card.classList.add("revealed");
    if (!RECORDING) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function foot(text) {
    $("foot-status").textContent = text;
  }

  function fmtBool(v, yes, no) {
    if (v === null || v === undefined) return "—";
    return v ? yes : no;
  }

  function fmtJson(value) {
    return JSON.stringify(value);
  }

  function showView(view, caseId, { restore = false } = {}) {
    if (RECORDING) $("presenter").hidden = tourActive() || view !== "demo";
    $("page-demo").hidden = view !== "demo";
    $("page-cases").hidden = view !== "cases";
    $("page-compare").hidden = view !== "compare";
    $("page-ablation").hidden = view !== "ablation";
    document.querySelectorAll(".view-button").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    if (restore) return; // Restore content separately without reloads or animated scrolling.
    if (view === "cases" && showcaseData) {
      selectCase(caseId || showcaseData.cases[0].case_id);
    }
    if (view === "cases") loadFeaturedCase();
    if (view === "compare") loadComparison();
    if (view === "ablation") loadAblation();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  let featuredData = null;
  let featuredRequest = null;
  let featuredLoadVersion = 0;
  let featuredSelection = ["tuple_str_int", "find_char_long"].includes(params.get("case"))
    ? params.get("case") : "tuple_str_int";

  function highlightedText(node, text, quote) {
    const index = text.indexOf(quote);
    if (!quote || index < 0) { node.textContent = text; return; }
    node.appendChild(document.createTextNode(text.slice(0, index)));
    node.appendChild(el("mark", "featured-mark", quote));
    node.appendChild(document.createTextNode(text.slice(index + quote.length)));
  }

  function renderFeaturedCase() {
    const item = featuredData.cases.find(c => c.id === featuredSelection);
    if (!item) return;
    const root = $("featured-detail");
    root.textContent = "";
    root.hidden = false;
    document.querySelectorAll("[data-featured-case]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.featuredCase === item.id));
    });
    root.appendChild(el("span", "eyebrow", item.kind));
    const heading = el("h2", null, `${item.id}：${item.title}`);
    heading.id = "featured-case-title";
    heading.tabIndex = -1;
    root.appendChild(heading);
    root.appendChild(el("p", "featured-conclusion", item.conclusion));
    const facts = el("div", "featured-facts");
    const a = item.assessment;
    for (const text of [
      item.functional.base_status === "pass" && item.functional.plus_status === "pass"
        ? "官方历史功能：base / plus 均通过" : "功能证据：不可用，变异代码未执行",
      `审核过程结论：${a.process_correct ? "成立" : "有问题"}`,
      `审核首错层级：${a.first_faulty_layer} · ${a.error_type}`,
      `审核首错步骤：${a.first_faulty_step || "null（未指定）"}`,
    ]) facts.appendChild(el("span", null, text));
    root.appendChild(facts);
    root.appendChild(el("h3", null, "公开需求原文"));
    root.appendChild(el("pre", "featured-original", item.problem.requirement));
    const pair = el("div", "featured-pair");
    const claim = el("section");
    claim.appendChild(el("h3", null, item.claim.label));
    const claimText = el("p", "featured-claim");
    highlightedText(claimText, item.claim.text, item.claim.highlight);
    claim.appendChild(claimText);
    const code = el("section");
    code.appendChild(el("h3", null, `代码原文 · 相关实现 L${item.code_line}`));
    const pre = el("pre", "featured-original");
    item.solution.code.split("\n").forEach((line, index) => {
      const row = el("span", "code-line");
      row.appendChild(el("span", "lineno", String(index + 1)));
      if (index + 1 === item.code_line) highlightedText(row, line, item.code_highlight);
      else row.appendChild(document.createTextNode(line));
      pre.appendChild(row);
    });
    code.appendChild(pre);
    pair.append(claim, code);
    root.appendChild(pair);
    root.appendChild(el("p", "boundary-note", item.analysis));
    const limits = el("ul", "featured-limits");
    item.limitations.forEach(text => limits.appendChild(el("li", null, text)));
    root.appendChild(limits);
    if (item.judgments.length) {
      const judgments = el("details", "featured-evidence");
      judgments.appendChild(el("summary", null, "既有两方法判断：代码定位 L2，步骤首错均为 null"));
      for (const j of item.judgments) {
        judgments.appendChild(el("h3", null, j.method));
        judgments.appendChild(el("p", "fineprint", `运行：${j.run_id} · Judge 原始判断 · 首错步骤：${j.assessment.first_faulty_step || "null"}`));
        judgments.appendChild(el("blockquote", null, j.assessment.explanation));
        judgments.appendChild(el("code", null, j.assessment.first_faulty_location.quote));
      }
      root.appendChild(judgments);
    }
    const original = el("details", "featured-evidence");
    original.appendChild(el("summary", null, "展开完整解答原文与审核定位"));
    original.appendChild(el("pre", "featured-original", JSON.stringify({solution:item.solution,reviewed_assessment:item.assessment}, null, 2)));
    root.appendChild(original);
    const sources = el("details", "featured-evidence");
    sources.appendChild(el("summary", null, "证据来源与 SHA-256（可核对）"));
    sources.appendChild(el("p", "fineprint", `案例摘录版本 ${featuredData.date} · 内容 SHA-256 ${featuredData.bundle_sha256}`));
    if (item.functional.run_id) sources.appendChild(el("p", null, `官方历史功能运行：${item.functional.run_id}`));
    for (const key of item.source_ids) {
      const source = featuredData.sources[key];
      sources.appendChild(el("p", "featured-source", source.path));
      sources.appendChild(el("p", "fineprint", `来源字节 SHA-256：${source.sha256}`));
      sources.appendChild(el("p", "fineprint", source.verification === "local_content_verified"
        ? "本机来源内容已核对（兼容换行转换）；不代表重新执行或重新评审。"
        : "展示已发布摘录；本机缺少原始材料，未重新核验原文件。"));
    }
    root.appendChild(sources);
    const links = el("p", "overview-entry");
    const review = el("a", "download-link", "查看审核说明");
    review.href = item.review_href;
    review.target = "_blank";
    review.rel = "noopener";
    const permalink = el("a", "download-link", "此案例直接入口");
    permalink.href = `/?case=${item.id}`;
    const download = el("a", "download-link", "下载案例与来源 JSON");
    download.href = "/api/featured-process-cases";
    download.download = "featured_process_cases_v1.json";
    links.append(review, permalink, download);
    root.appendChild(links);
    $("featured-status").textContent = `${item.id} · 已加载核验摘录 · 静态对照，未新增执行`;
  }

  async function loadFeaturedCase(id) {
    const version = ++featuredLoadVersion;
    if (id) featuredSelection = id;
    $("featured-detail").hidden = true;
    $("featured-status").textContent = "正在核对重点案例来源…";
    try {
      if (!featuredData) {
        if (!featuredRequest) featuredRequest = fetch("/api/featured-process-cases").then(async response => {
          const data = await response.json();
          if (!response.ok || !data.ok) throw new Error("unavailable");
          return data;
        });
        featuredData = await featuredRequest;
      }
      if (version !== featuredLoadVersion) return false;
      renderFeaturedCase();
      return true;
    } catch {
      if (version !== featuredLoadVersion) return false;
      featuredRequest = null;
      $("featured-detail").textContent = "";
      document.querySelectorAll("[data-featured-case]").forEach(button => button.setAttribute("aria-pressed", "false"));
      $("featured-status").textContent = "重点案例来源暂不可用或校验未通过。可重新点击重试；下方公开 Fixture 仍可独立查看。";
      return false;
    }
  }

  function configureExports(runId, hasCertificate) {
    currentRunId = runId;
    const base = `/api/export/${runId}`;
    const links = [
      ["export-json", `${base}/result.json`, `tracejudge_${runId}_result.json`],
      ["export-html", `${base}/report.html`, `tracejudge_${runId}_report.html`],
      ["export-certificate", `${base}/certificate.json`, `tracejudge_${runId}_certificate.json`],
    ];
    for (const [id, href, filename] of links) {
      const link = $(id);
      link.href = href;
      link.setAttribute("download", filename);
    }
    $("export-certificate").hidden = !hasCertificate;
    $("export-actions").hidden = false;
    updatePresenter();
  }

  function renderCode(code, span) {
    const pre = el("pre", "code");
    const match = typeof span === "string" && /^L(\d+)(?:-L?(\d+))?$/.exec(span);
    const start = match ? Number(match[1]) : 0;
    const end = match ? Number(match[2] || match[1]) : 0;
    code.split("\n").forEach((line, index) => {
      const row = el("span", `code-line${index + 1 >= start && index + 1 <= end ? " located" : ""}`);
      row.appendChild(el("span", "lineno", String(index + 1)));
      row.appendChild(document.createTextNode(line || " "));
      pre.appendChild(row);
    });
    return pre;
  }

  // ------------------------------------------------------------ renderers

  function renderProblem(problem) {
    const body = $("problem-body");
    body.textContent = "";
    const dl = el("dl", "kv");
    kv(dl, "题目", `${problem.title}（${problem.problem_id}）`);
    kv(dl, "函数签名", problem.function_signature, true);
    kv(dl, "需求", problem.requirement);
    if (problem.test_counts) {
      const c = problem.test_counts;
      kv(dl, "测试用例", `可见 ${c.visible} · 边界(hidden) ${c.hidden} · 挑战 ${c.challenge}`, true);
    }
    if (problem.source) kv(dl, "来源", problem.source, true);
    body.appendChild(dl);
    if (problem.requirements) {
      const ul = el("ul", "req-list");
      for (const item of problem.requirements) {
        const li = el("li");
        li.appendChild(el("b", null, item.requirement_id));
        li.appendChild(document.createTextNode(item.content));
        ul.appendChild(li);
      }
      body.appendChild(ul);
    }
  }

  function renderSolution(data) {
    const body = $("solution-body");
    body.textContent = "";
    const s = data.solution;
    const dl = el("dl", "kv");
    kv(dl, "需求理解", s.requirement_understanding);
    kv(dl, "设计摘要", s.design_summary);
    if (s.edge_cases_considered && s.edge_cases_considered.length) {
      kv(dl, "考虑的边界", s.edge_cases_considered.join("；"));
    }
    if (s.declared_time_complexity) kv(dl, "声明时间复杂度", s.declared_time_complexity, true);
    body.appendChild(dl);

    if (s.implementation_steps && s.implementation_steps.length) {
      const ul = el("ul", "step-list");
      for (const step of s.implementation_steps) {
        const li = el("li");
        li.appendChild(el("b", null, step.step_id));
        li.appendChild(document.createTextNode(step.content));
        if (step.related_requirements && step.related_requirements.length) {
          li.appendChild(el("span", "refs", `（关联 ${step.related_requirements.join("、")}）`));
        }
        ul.appendChild(li);
      }
      body.appendChild(ul);
    }

    body.appendChild(renderCode(s.code));
  }

  function renderAlignment(data) {
    const body = $("alignment-body");
    body.textContent = "";
    const se = data.static_evidence;
    const a = data.assessment;
    const dl = el("dl", "kv");
    kv(dl, "AST 解析", fmtBool(se.ast_parse_ok, "成功", "失败"));
    kv(dl, "if 分支数", se.if_count, true);
    kv(dl, "空输入判断", fmtBool(se.has_empty_input_check, "检测到", "未发现"), true);
    kv(dl, "可疑硬编码", fmtBool(se.suspicious_hardcoding, "是", "否"));
    if (se.data_structures_used && se.data_structures_used.length) {
      kv(dl, "数据结构", se.data_structures_used.join(", "), true);
    }
    kv(dl, "首错层", a.first_faulty_layer || "（未发现）", true);
    body.appendChild(dl);
    const note = el(
      "p",
      "explanation",
      "四层对齐同时核对：需求—推理、推理内部一致性、推理—代码、代码—执行证据；" +
        "规则证据与模型判断交叉验证。"
    );
    body.appendChild(note);
  }

  function renderExecution(data) {
    const body = $("execution-body");
    body.textContent = "";
    const ex = data.execution;
    const labels = { visible: "可见", hidden: "边界(hidden)", challenge: "挑战" };
    const agg = el("div", "agg");
    for (const cat of ["visible", "hidden", "challenge"]) {
      const c = ex.categories[cat];
      if (!c || !c.total) continue;
      const item = el("span", c.passed === c.total ? "ok" : "bad");
      item.appendChild(document.createTextNode(`${labels[cat]} `));
      item.appendChild(el("b", null, `${c.passed}/${c.total} 通过`));
      agg.appendChild(item);
    }
    const status = el("span");
    status.appendChild(document.createTextNode("运行状态 "));
    status.appendChild(el("b", null, ex.runtime_status));
    agg.appendChild(status);
    body.appendChild(agg);

    const table = el("table", "test-table");
    const head = el("tr");
    for (const h of ["CASE", "TYPE", "ARGS", "EXPECTED", "ACTUAL", "RESULT"]) {
      head.appendChild(el("th", null, h));
    }
    table.appendChild(head);
    for (const r of ex.results) {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", r.case_id));
      tr.appendChild(el("td", null, labels[r.category] || r.category));
      tr.appendChild(el("td", "mono", fmtJson(r.args === undefined ? "" : r.args)));
      tr.appendChild(el("td", "mono", fmtJson(r.expected_output)));
      tr.appendChild(
        el("td", "mono", r.exception_type ? `${r.exception_type}` : fmtJson(r.actual_output))
      );
      const res = el(
        "td",
        null,
        ""
      );
      const tag = el(
        "span",
        r.passed ? "result-pass" : "result-fail",
        r.passed ? "PASS" : r.timed_out ? "TIMEOUT" : "FAIL"
      );
      res.appendChild(tag);
      tr.appendChild(res);
      table.appendChild(tr);
    }
    body.appendChild(table);
    const note = el(
      "p",
      "fineprint",
      "注：hidden / challenge 是该公开自建 Fixture 内部的测试类别名称，不是第三方隐藏测试正文。"
    );
    body.appendChild(note);
  }

  function renderAssessment(data) {
    const body = $("assessment-body");
    body.textContent = "";
    const a = data.assessment;

    const line = el("div", "verdict-line");
    line.appendChild(
      el(
        "span",
        `verdict-main ${a.process_correct ? "good" : "bad"}`,
        a.process_correct ? "过程成立" : "过程不成立"
      )
    );
    if (a.error_type) line.appendChild(el("span", "pill", a.error_type));
    body.appendChild(line);

    const dl = el("dl", "kv");
    kv(dl, "功能判断", fmtBool(a.functional_correct, "正确", "存在问题"));
    kv(dl, "过程判断", fmtBool(a.process_correct, "成立", "不成立"));
    kv(dl, "首错层", a.first_faulty_layer, true);
    kv(dl, "首错步骤", a.first_faulty_step, true);
    kv(dl, "关联需求", a.violated_requirement, true);
    kv(dl, "错误类型", a.error_type, true);
    kv(dl, "代码位置 code_span", a.code_span, true);
    if (a.secondary_error_types && a.secondary_error_types.length) {
      kv(dl, "次要错误类型", a.secondary_error_types.join(", "), true);
    }
    const evidenceGrid = el("div", "assessment-grid");
    evidenceGrid.appendChild(dl);
    const located = el("section");
    const locatedStep = data.solution.implementation_steps.find((step) => step.step_id === a.first_faulty_step);
    if (locatedStep) {
      located.appendChild(el("p", "located-step", `${locatedStep.step_id} · ${locatedStep.content}`));
    }
    if (a.code_span) {
      located.appendChild(renderCode(data.solution.code, a.code_span));
      located.appendChild(el("p", "fineprint", "标记为评估器报告的代码范围；范围可能覆盖整个函数，不等同于精确错误行。"));
    }
    evidenceGrid.appendChild(located);
    body.appendChild(evidenceGrid);
    if (a.explanation) {
      const reasoning = el("div", "explanation");
      reasoning.appendChild(el("small", null, `评估记录原文 · 本次评审来源：${data.mode === "fixture" ? "Mock（非真实 Hy3）" : "Hy3"}`));
      reasoning.appendChild(el("p", null, a.explanation));
      body.appendChild(reasoning);
    }
  }

  function renderCertificate(data) {
    const body = $("certificate-body");
    body.textContent = "";
    const ce = data.counterexample;
    const cert = data.certificate;

    if (ce) {
      const box = el("div", "counterexample-box");
      box.appendChild(el("span", null, ce.minimized ? "经最小化处理的反例" : "可重放反例（未标记为已最小化）"));
      box.appendChild(el("strong", null, `args = ${fmtJson(ce.args)}`));
      const meta = el("p", "mono");
      meta.textContent =
        `expected  ${fmtJson(ce.expected)}\n` +
        `actual    ${ce.candidate_exception || fmtJson(ce.candidate_output)}\n` +
        `source    ${ce.source}${ce.minimized ? "（已最小化）" : ""}`;
      meta.style.whiteSpace = "pre-line";
      meta.style.color = "#c3d0dd";
      box.appendChild(meta);
      body.appendChild(box);
    } else {
      body.appendChild(el("p", null, "本次运行未产生独立反例。"));
    }

    const dl = el("dl", "kv");
    dl.style.marginTop = "14px";
    if (cert) {
      kv(dl, "证书等级 verdict", cert.verdict, true);
      kv(dl, "error_type", cert.error_type, true);
      kv(dl, "violated_requirement", cert.violated_requirement, true);
      kv(dl, "first_faulty_step", cert.first_faulty_step, true);
    } else {
      kv(dl, "错误证书", "本次未产生错误证书；以过程判断与证据说明为准");
    }
    const replay = data.replay;
    if (replay) {
      kv(
        dl,
        "证书重放",
        replay.applicable
          ? fmtBool(replay.reproduced, "重放成功：失败可复现", "重放未复现失败")
          : "不适用",
        true
      );
    }
    kv(dl, "结果文件", data.artifact_relpath, true);
    kv(dl, "本次运行耗时", `${data.duration_seconds}s`, true);
    body.appendChild(dl);
    if (replay && replay.detail) body.appendChild(el("p", "explanation", replay.detail));
  }

  function renderError(data) {
    runData = null;
    runFailed = true;
    renderSampleCosts(data.cost_metrics);
    if (currentRunId) configureExports(currentRunId, false);
    $("execution-notice").textContent = "本次执行未完成。下面显示实际错误，不替换为演示结果。";
    const body = $("solution-body");
    body.textContent = "";
    const box = el("div", "error-box");
    box.appendChild(el("b", null, "运行未完成"));
    box.appendChild(document.createTextNode(`：${data.error}`));
    box.appendChild(el("small", null, `error_type = ${data.error_type} · 页面未切换到 Mock，未展示预设结果`));
    body.appendChild(box);
    revealCard("card-solution");
    selectScene(1);
  }

  function renderOverview(o, shouldScroll) {
    const section = $("overview");
    section.hidden = false;
    overviewReady = true;
    $("overview-source").textContent =
      o.source === "structured_artifact"
        ? "数据来源：哈希绑定的结构化公开聚合产物"
        : "数据来源：仓库已发布的公开 Markdown 汇总";

    const grid = $("overview-grid");
    grid.textContent = "";
    const cells = [
      [String(o.trace_count), "冻结研究轨迹（42 自然 + 15 反事实）"],
      [String(o.pair_count), "严格配对判断（5 种方法）"],
      [
        `${(o.best_detection.accuracy * 100).toFixed(1)}%`,
        `最佳观察检测准确率 ${o.best_detection.numerator}/${o.best_detection.denominator}（${o.best_detection.method}）`,
      ],
      [
        `${(o.full_false_positive_rate.rate * 100).toFixed(2)}%`,
        `Full TraceJudge 误报率 ${o.full_false_positive_rate.numerator}/${o.full_false_positive_rate.denominator}`,
      ],
    ];
    for (const [num, label] of cells) {
      const cell = el("div", "cell");
      cell.appendChild(el("strong", null, num));
      cell.appendChild(el("span", null, label));
      grid.appendChild(cell);
    }

    const extra = $("overview-extra");
    extra.textContent = "";
    const review = o.human_review;
    const r1 = el("div");
    r1.appendChild(el("b", null, `${review.primary_labeled}/${review.primary_total}`));
    r1.appendChild(document.createTextNode(" 第一标注者盲法标签覆盖（已冻结）"));
    extra.appendChild(r1);
    const r2 = el("div");
    r2.appendChild(el("b", null, `${review.second_completed}/${review.second_planned}`));
    r2.appendChild(
      document.createTextNode(` 第二标注者独立复标；${review.agreement_status === "computed" ? "一致性已计算" : "一致性尚未计算"}`)
    );
    extra.appendChild(r2);
    for (const row of o.difficulty) {
      const d = el("div");
      d.appendChild(el("b", null, `${row.passed}/${row.included}`));
      d.appendChild(document.createTextNode(` ${row.tier} Base+Plus 通过（代理难度分层）`));
      extra.appendChild(d);
    }
    $("overview-disclaimer").textContent = o.disclaimer;
    if (shouldScroll && !RECORDING) section.scrollIntoView({ behavior: "smooth", block: "nearest" });
    updatePresenter();
  }

  // Shared read-only entry to the contest results overview.  The server serves
  // the Markdown as plain text at a fixed allowlisted route, so the link can
  // never 404 while the document exists in the repo; the repo-relative path is
  // shown alongside for offline readers.
  function resultsOverviewEntry(repoPath, href) {
    const line = el("p", "overview-entry");
    const link = el("a", "download-link", "成果总览（只读）");
    link.href = href || "/docs/contest-results-overview";
    link.target = "_blank";
    link.rel = "noopener";
    line.appendChild(link);
    line.appendChild(el("span", "fineprint", `仓库相对路径：${repoPath || "docs/contest_results_overview.md"}`));
    return line;
  }

  function renderCurrentValidation(data) {
    currentValidationReady = true;
    const body = $("current-validation-body");
    body.textContent = "";
    $("current-validation-source").textContent =
      data.verification === "artifacts_verified"
        ? "已与本地绑定产物逐项核验（SHA-256 + 关键计数）"
        : "展示仓库发布的聚合摘要（本地实验产物未随仓库分发）";
    for (const card of data.experiments) {
      const box = el("section", "cv-card");
      box.appendChild(el("h3", null, card.title));
      box.appendChild(el("p", "fineprint", card.scope));
      const ul = el("ul", "cv-lines");
      for (const line of card.lines) ul.appendChild(el("li", null, line));
      box.appendChild(ul);
      const limits = el("ul", "cv-limits");
      for (const limit of card.limitations) limits.appendChild(el("li", null, limit));
      box.appendChild(limits);
      body.appendChild(box);
    }
    const globalNote = el("p", "disclaimer", data.global_limitations.join(" "));
    body.appendChild(globalNote);
    body.appendChild(resultsOverviewEntry(data.overview_entry.repo_path, data.overview_entry.href));
  }

  function renderCurrentValidationUnavailable() {
    currentValidationReady = false;
    const body = $("current-validation-body");
    body.textContent = "";
    $("current-validation-source").textContent = "来源未通过校验";
    const box = el("div", "error-box");
    box.appendChild(el("b", null, "最新补充验证暂不可用；历史冻结结果仍可查看"));
    box.appendChild(el("small", null, "来源缺失或未通过 SHA-256 / schema / 关键计数一致性核验；页面不填零、不猜测、不回退成看似真实的新成绩。"));
    body.appendChild(box);
    body.appendChild(resultsOverviewEntry());
  }

  async function loadCurrentValidation() {
    try {
      const resp = await fetch("/api/current-validation-summary");
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error("current validation unavailable");
      renderCurrentValidation(data);
    } catch {
      renderCurrentValidationUnavailable();
    }
  }

  function renderCaseList(data) {
    const list = $("case-list");
    list.textContent = "";
    for (const item of data.cases) {
      const button = el("button", "case-option");
      button.dataset.caseId = item.case_id;
      button.appendChild(el("span", "case-kind", item.mutation_kind));
      button.appendChild(el("b", null, item.title));
      button.appendChild(el("small", null, item.short_label));
      button.addEventListener("click", () => selectCase(item.case_id));
      list.appendChild(button);
    }
  }

  function renderContrast() {
    const item = showcaseData.cases.find((entry) => entry.case_id === "reasoning_swap");
    if (!item) return;
    const root = $("contrast-body");
    root.textContent = "";
    const badges = el("div", "agg");
    badges.appendChild(el("span", item.functional_correct ? "result-pass" : "result-fail",
      item.functional_correct ? "功能：公开测试通过" : "功能：存在问题"));
    badges.appendChild(el("span", item.process_correct ? "result-pass" : "result-fail",
      item.process_correct ? "过程：成立" : "过程：说明与代码矛盾"));
    root.appendChild(badges);
    const grid = el("div", "contrast-grid");
    const explanation = el("section");
    explanation.appendChild(el("h3", null, "这份解答声称"));
    explanation.appendChild(el("p", "explanation", item.solution.requirement_understanding));
    const steps = el("ul", "step-list");
    for (const step of item.solution.implementation_steps) {
      const li = el("li");
      if (step.step_id === item.assessment.first_faulty_step) li.className = "faulty";
      li.appendChild(el("b", null, step.step_id));
      li.appendChild(document.createTextNode(step.content));
      steps.appendChild(li);
    }
    explanation.appendChild(steps);
    const implementation = el("section");
    implementation.appendChild(el("h3", null, "代码实际实现"));
    implementation.appendChild(renderCode(item.solution.code));
    implementation.appendChild(el("p", "fineprint", item.execution.summary));
    grid.append(explanation, implementation);
    root.appendChild(grid);
    root.appendChild(el("p", "explanation", item.summary));
    root.appendChild(el("p", "fineprint", "来自哈希校验的公开构造案例与既有证据；切换到本幕不会再次调用模型或执行测试。"));
  }

  function renderCaseDetail(item) {
    const root = $("case-detail");
    root.textContent = "";
    const head = el("div", "case-detail-head");
    const heading = el("div");
    heading.appendChild(el("span", "eyebrow", item.mutation_kind));
    heading.appendChild(el("h2", null, item.title));
    heading.appendChild(el("p", null, item.summary));
    head.appendChild(heading);
    const verdict = el(
      "span",
      `case-verdict ${item.process_correct ? "good" : "bad"}`,
      item.process_correct ? "过程正确" : "过程错误"
    );
    head.appendChild(verdict);
    root.appendChild(head);

    const chain = el("div", "evidence-chain");
    const problem = el("section", "chain-block");
    problem.appendChild(el("span", "chain-index", "01 · 题目"));
    problem.appendChild(el("h3", null, `${item.problem.title}（${item.problem.problem_id}）`));
    problem.appendChild(el("p", null, item.problem.requirement));
    problem.appendChild(el("code", "signature", item.problem.function_signature));
    chain.appendChild(problem);

    const reasoning = el("section", "chain-block");
    reasoning.appendChild(el("span", "chain-index", "02 · 推理轨迹"));
    reasoning.appendChild(el("h3", null, "需求理解与实现步骤"));
    reasoning.appendChild(el("p", null, item.solution.requirement_understanding));
    const steps = el("ul", "step-list");
    for (const step of item.solution.implementation_steps) {
      const li = el("li");
      if (!item.process_correct && step.step_id === item.assessment.first_faulty_step) {
        li.classList.add("faulty");
      }
      li.appendChild(el("b", null, step.step_id));
      li.appendChild(document.createTextNode(step.content));
      steps.appendChild(li);
    }
    reasoning.appendChild(steps);
    chain.appendChild(reasoning);

    const code = el("section", "chain-block");
    code.appendChild(el("span", "chain-index", "03 · 生成代码"));
    code.appendChild(el("h3", null, item.mutation.sole_change));
    const pre = el("pre", "code");
    item.solution.code.split("\n").forEach((line, index) => {
      pre.appendChild(el("span", "lineno", String(index + 1)));
      pre.appendChild(document.createTextNode(`${line}\n`));
    });
    code.appendChild(pre);
    chain.appendChild(code);

    const execution = el("section", "chain-block");
    execution.appendChild(el("span", "chain-index", "04 · 执行证据"));
    const status = el(
      "span",
      `execution-status ${item.execution.status === "pass" ? "good" : "bad"}`,
      item.execution.status.toUpperCase()
    );
    execution.appendChild(status);
    execution.appendChild(el("p", null, item.execution.summary));
    if (item.execution.selected_case) {
      const c = item.execution.selected_case;
      const preEvidence = el("pre", "evidence-code");
      preEvidence.textContent =
        `case      ${c.case_id}\nargs      ${fmtJson(c.args)}\nexpected  ${fmtJson(c.expected)}\nactual    ${c.actual}`;
      execution.appendChild(preEvidence);
    }
    chain.appendChild(execution);
    root.appendChild(chain);

    const conclusion = el("section", "case-conclusion");
    conclusion.appendChild(el("h3", null, "评估结论与证据边界"));
    const dl = el("dl", "kv");
    kv(dl, "functional_correct", fmtBool(item.functional_correct, "true", "false"), true);
    kv(dl, "process_correct", fmtBool(item.process_correct, "true", "false"), true);
    kv(dl, "first_faulty_layer", item.assessment.first_faulty_layer || "（无已证实错误）", true);
    kv(dl, "first_faulty_step", item.assessment.first_faulty_step || "（无已证实错误）", true);
    kv(dl, "error_type", item.assessment.error_type || "（无已证实错误）", true);
    kv(dl, "certificate", item.certificate.verdict, true);
    kv(dl, "evidence_mode", item.certificate.evidence_mode, true);
    conclusion.appendChild(dl);
    conclusion.appendChild(el("p", "explanation", item.assessment.rationale));
    if (item.certificate.boundary_note) {
      conclusion.appendChild(el("p", "boundary-note", item.certificate.boundary_note));
    }
    const observation = el("div", "observation");
    observation.appendChild(el("b", null, "同类聚合观察"));
    observation.appendChild(
      el("span", null, `Test-only ${item.cohort_observation.test_only} · ${item.cohort_observation.judge_methods}`)
    );
    observation.appendChild(el("small", null, item.cohort_observation.scope));
    conclusion.appendChild(observation);
    root.appendChild(conclusion);
  }

  function selectCase(caseId) {
    if (!showcaseData) return;
    const selected = showcaseData.cases.find((item) => item.case_id === caseId) || showcaseData.cases[0];
    document.querySelectorAll(".case-option").forEach((button) => {
      const active = button.dataset.caseId === selected.case_id;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    renderCaseDetail(selected);
  }

  function metricText(metric) {
    if (metric.numerator !== undefined) return `${metric.numerator}/${metric.denominator}`;
    if (metric.count !== undefined) {
      return metric.denominator !== undefined ? `${metric.count}/${metric.denominator}` : String(metric.count);
    }
    if (metric.status === "not_computable") return "不可计算";
    if (metric.status === "not_recorded") return "未单列";
    return "—";
  }

  function renderRegression(card) {
    regressionData = card;
    const grid = $("regression-grid");
    grid.textContent = "";
    const labels = {
      error_detection: "公开有错样例检出",
      false_positive_count: "等价实现误报",
      exact_first_step_localization: "精确首错定位",
      counterexample_replay_pass: "反例重放",
      provider_failure_count: "Provider 失败",
      abstention_count: "弃权 / abstention",
    };
    const version = el("div", "regression-cell version");
    version.appendChild(el("span", null, "EVALUATOR VERSION"));
    version.appendChild(el("strong", null, card.evaluator_version));
    version.appendChild(el("small", null, `${card.fixture_count} fixtures · ${card.verification_status}/${card.confidence}`));
    grid.appendChild(version);
    for (const [key, label] of Object.entries(labels)) {
      const metric = card.metrics[key];
      const cell = el("div", "regression-cell");
      cell.dataset.regressionMetric = key;
      cell.appendChild(el("span", null, label));
      cell.appendChild(el("strong", null, metricText(metric)));
      cell.appendChild(el("small", null, metric.scope || metric.reason));
      grid.appendChild(cell);
    }
    $("regression-boundary").textContent = card.boundary;
  }

  async function loadShowcase() {
    try {
      const resp = await fetch("/api/showcase");
      if (!resp.ok) throw new Error("showcase unavailable");
      showcaseData = await resp.json();
      renderContrast();
      updatePresenter();
      renderCaseList(showcaseData);
      selectCase(showcaseData.cases[0].case_id);
      $("case-source").textContent = `公开源已校验 · SHA256 ${showcaseData.source.counterfactual_sha256.slice(0, 12)}…`;
      $("cases-disclaimer").textContent = showcaseData.disclaimer;
    } catch {
      $("case-source").textContent = "公开源校验失败";
      $("case-detail").textContent = "案例数据不可用；页面不会用硬编码结果替代。";
    }
  }

  async function loadRegression() {
    regressionData = null;
    try {
      const resp = await fetch("/api/regression");
      if (!resp.ok) throw new Error("regression unavailable");
      renderRegression(await resp.json());
    } catch {
      regressionData = null;
      $("regression-grid").textContent = "回归卡片生成失败；未展示占位指标。";
    }
  }

  // ------------------------------------------------------------- run flow

  function costTable(headers, rows) {
    const wrap = el("div", "cost-table-wrap");
    wrap.tabIndex = 0;
    const table = el("table", "cost-table");
    const head = el("thead");
    const hr = el("tr");
    headers.forEach((label) => { const th = el("th", null, label); th.scope = "col"; hr.appendChild(th); });
    head.appendChild(hr);
    table.appendChild(head);
    const body = el("tbody");
    rows.forEach((cells) => { const tr = el("tr"); cells.forEach((value) => tr.appendChild(el("td", null, value))); body.appendChild(tr); });
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

  const costNumber = (value) => value == null ? "未返回" : Number(value).toLocaleString("zh-CN");
  const costSeconds = (value) => value == null ? "未采集" : `${Number(value).toFixed(3)} s`;

  function renderSampleCosts(costs) {
    const root = $("sample-costs");
    root.textContent = "";
    if (!costs) {
      root.appendChild(el("p", "placeholder", "运行一条样本后显示 Solver 与 Judge 用量；历史运行未记录的数据无法补算。"));
      return;
    }
    root.appendChild(el("p", "cost-source", `${costs.mode === "fixture" ? "公开 Fixture · 无模型 API 调用" : "真实 Hy3 · 当次请求用量"} · Run ${currentRunId || "—"} · 样本 ${costs.sample_id.slice(0, 12)}`));
    const totals = el("div", "cost-totals");
    for (const [label, value] of [["样本总耗时", costs.total_seconds], ["评测总耗时（排除 Solver）", costs.evaluation_seconds], ["其中：证书重放", costs.replay_seconds]]) {
      const item = el("div"); item.appendChild(el("span", null, label)); item.appendChild(el("strong", null, costSeconds(value))); totals.appendChild(item);
    }
    root.appendChild(totals);
    root.appendChild(costTable(["阶段", "输入 token", "输出 token", "模型请求耗时", "阶段耗时", "调用次数", "重试次数"],
      ["solver", "judge"].map((role) => { const r = costs.roles[role]; return [role === "solver" ? "Solver 生成" : "Judge 评审", costNumber(r.input_tokens), costNumber(r.output_tokens), costSeconds(r.request_seconds), costSeconds(r.wall_seconds), r.calls, r.retries]; })));
    root.appendChild(el("p", "cost-note", costs.scope));
    root.appendChild(el("p", "cost-note", costs.token_scope));
    for (const role of ["solver", "judge"]) {
      const r = costs.roles[role];
      if (r.input_tokens == null || r.output_tokens == null) root.appendChild(el("p", "boundary-note", `${role}: 已知输入 ${costNumber(r.known_input_tokens)}（${r.input_tokens_coverage}/${r.calls} 请求有记录）；已知输出 ${costNumber(r.known_output_tokens)}（${r.output_tokens_coverage}/${r.calls}）。缺失用量未按 0 计入总数。`));
      if (r.failed_operations) root.appendChild(el("p", "boundary-note", `${role}: ${r.failed_operations} 次阶段执行失败；已发生的请求仍保留。请结合本次评估结果判断是否采用了降级结果。`));
    }
    const detail = el("details", "cost-requests");
    detail.appendChild(el("summary", null, `逐次模型请求明细（${costs.requests.length} 次）`));
    if (costs.requests.length) detail.appendChild(costTable(["序号", "角色", "阶段内尝试", "输入 token", "输出 token", "请求耗时", "请求状态"], costs.requests.map((r) => [r.request_id, r.role, `${r.attempt}${r.is_retry ? " · 重试" : ""}`, costNumber(r.input_tokens), costNumber(r.output_tokens), costSeconds(r.request_seconds), r.error_type || "已返回（解析结果见阶段状态）"])));
    else detail.appendChild(el("p", "cost-note", costs.mode === "fixture" ? "Fixture 使用 Mock，真实 API 调用和 token 均为 0；本地执行耗时已测量。" : "本次尚未发起模型请求。"));
    root.appendChild(detail);
    root.appendChild(el("p", "cost-note", costs.persistence_ok ? `逐次记录已保存：${costs.artifact_relpath}；结果 JSON 包含同一份明细。` : "本地用量日志写入失败，请下载结果 JSON 保留当前明细。"));
  }

  async function loadMethodCosts() {
    const root = $("method-costs");
    try {
      const response = await fetch("/api/method-costs");
      if (!response.ok) throw new Error("method costs unavailable");
      const data = await response.json();
      root.textContent = "";
      root.appendChild(el("p", "cost-source", `已发布阶段三实验 · ${data.cohort} · 非本次 Demo 数据`));
      root.appendChild(costTable(["方法", "检测准确率", "有效判断 / 总数", "已知输入 token（覆盖行）", "已知输出 token（覆盖行）", "调用 / JSON 修复", "评测累计耗时"], data.rows.map((r) => [r.method, `${r.correct}/${r.total} · ${(r.accuracy * 100).toFixed(1)}%`, `${r.valid}/${r.total}${r.provider_failures ? ` · ${r.provider_failures} 次失败` : ""}`, `${costNumber(r.known_input_tokens)}（${r.input_coverage}/${r.total}）`, `${costNumber(r.known_output_tokens)}（${r.output_coverage}/${r.total}）`, `${r.calls} / ${r.json_repairs}`, `${r.duration_sum_seconds.toFixed(1)} s`])));
      root.appendChild(el("p", "cost-note", data.scope));
      root.appendChild(el("p", "boundary-note", data.boundary));
      root.appendChild(el("p", "cost-note", `来源：${data.source}`));
    } catch {
      root.textContent = "公开报告成本表暂不可用；未填入估算值。";
    }
  }

  function resetRunUI() {
    renderSampleCosts(null);
    runData = null;
    runFailed = false;
    sceneIndex = 0;
    $("execution-notice").textContent = "尚未运行。下方步骤用于展示结果，不代表实时执行进度。";
    for (const id of ["card-solution", "card-alignment", "card-execution", "card-assessment", "card-certificate"]) {
      const card = $(id);
      card.classList.add("pending");
      card.classList.remove("revealed");
    }
    const placeholders = {
      "solution-body": "等待运行。",
      "alignment-body": "等待运行。",
      "execution-body": "等待运行。",
      "assessment-body": "等待运行。",
      "certificate-body": "等待运行。",
    };
    for (const [id, text] of Object.entries(placeholders)) {
      const body = $(id);
      body.textContent = "";
      body.appendChild(el("p", "placeholder", text));
    }
    for (let n = 1; n <= 6; n += 1) {
      setStage(n, n === 1 ? "done" : "");
    }
    resetCaptions();
    setCaption(1, "done");
    $("run-meta").textContent = "";
    currentRunId = null;
    $("export-actions").hidden = true;
    $("mode-badge").className = "mode-badge";
    $("mode-badge").textContent = "待开始";
    updatePresenter();
  }

  function setRunningUI(mode) {
    $("execution-notice").textContent = "流水线正在执行；当前仅显示总耗时，完成后再展示各阶段结果。";
    $("mode-badge").className = `mode-badge ${mode}`;
    $("mode-badge").textContent =
      mode === "fixture" ? "公开 FIXTURE · 未调用真实 HY3" : "真实 HY3 · DOCKER 沙盒";
    $("caption-generate").textContent =
      mode === "fixture" ? "Mock Solver 生成结构化解答" : "Hy3 生成结构化解答";
    for (const n of [2, 3, 4, 5, 6]) setStage(n, "");
    // The endpoint reports whole-run status, not live per-stage progress.
    foot("流水线真实执行中…");
    $("run-meta").textContent = "";
    updatePresenter();
  }

  async function revealResult(data) {
    runData = data;
    runFailed = false;
    $("execution-notice").textContent = "执行已完成。当前为本次结果的分步展示；切换画面不会重复运行。";
    const stages = [
      [2, "card-solution", () => renderSolution(data)],
      [3, "card-alignment", () => renderAlignment(data)],
      [4, "card-execution", () => renderExecution(data)],
      [5, "card-assessment", () => renderAssessment(data)],
      [6, "card-certificate", () => renderCertificate(data)],
    ];
    for (const [n, cardId, render] of stages) {
      setStage(n, "running");
      setCaption(n, "active");
      render();
      revealCard(cardId);
      if (!RECORDING) await new Promise((resolve) => setTimeout(resolve, REVEAL_DELAY_MS));
      setStage(n, "done");
      setCaption(n, "done");
    }
    const meta = $("run-meta");
    meta.textContent = "";
    const rows = [
      ["provider", data.provider.name],
      ["model", data.provider.model || "—"],
      ["sandbox", data.provider.sandbox],
      ["duration", `${data.duration_seconds}s`],
      ["artifact", data.artifact_relpath],
    ];
    for (const [k, v] of rows) {
      const line = el("div");
      line.appendChild(document.createTextNode(`${k}: `));
      line.appendChild(el("b", null, v));
      meta.appendChild(line);
    }
    foot("完成 · 结果来自当次真实运行");
    selectScene(1);
  }

  async function loadOverview(shouldScroll = false) {
    try {
      const resp = await fetch("/api/overview");
      if (!resp.ok) return;
      renderOverview(await resp.json(), shouldScroll);
    } catch {
      /* overview is optional; the run results stand on their own */
    }
  }

  async function pollRun(runId) {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      const resp = await fetch(`/api/run/${runId}`);
      if (!resp.ok) {
        running = false;
        $("btn-start").disabled = false;
        foot("状态查询失败");
        renderError({ error: "无法取得本次运行状态，请返回题目后重新检查服务。", error_type: "StatusError" });
        return;
      }
      const state = await resp.json();
      if (state.status === "running") {
        foot(`流水线真实执行中… ${state.elapsed_seconds}s`);
        continue;
      }
      const data = state.result;
      running = false;
      $("btn-start").disabled = false;
      renderSampleCosts(data && data.cost_metrics);
      if (!data || !data.ok) {
        renderError(data || { error: "未知错误", error_type: "UnknownError" });
        setStage(2, "failed");
        foot("运行失败（已如实展示，未切换为 Mock）");
        return;
      }
      await revealResult(data);
      configureExports(runId, Boolean(data.certificate));
      loadOverview(true);
      return;
    }
  }

  async function startRun() {
    if (running) return;
    running = true;
    $("btn-start").disabled = true;
    resetRunUI();
    const mode = document.querySelector('input[name="mode"]:checked').value;
    currentMode = mode;
    setRunningUI(mode);
    try {
      const resp = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: "请求被拒绝" }));
        throw new Error(err.error || "请求被拒绝");
      }
      const { run_id: runId } = await resp.json();
      currentRunId = runId;
      await pollRun(runId);
    } catch (exc) {
      running = false;
      $("btn-start").disabled = false;
      renderError({ error: `无法连接本地演示服务：${exc.message}`, error_type: "NetworkError" });
      setStage(2, "failed");
      foot("请求失败");
    }
  }

  // ---------------------------------------------------- method comparison
  // Read-only view of the pinned three-method pilot run.  Everything here is
  // rendered from /api/method-comparison with textContent only -- model text,
  // code and annotation rationales are untrusted strings, never HTML.

  let comparisonData = null;
  let comparisonRequested = false;
  let compareSelection = null;
  let compareFilter = "all";

  const COMPARE_FILTERS = [
    ["all", "全部样本", "不过滤：展示运行中的全部样本，避免只展示系统表现好的案例。"],
    ["disagreement", "方法分歧", "三种方法中，已完成判断（status=ok）的方法在任一可比字段上取值不同。调用失败、结果缺失或未知判断不算分歧。"],
    ["fp", "误报", "公开过程结论维度：某方法判为过程错误，而人工参考为过程正确。仅在人工参考明确时统计。"],
    ["fn", "漏报", "公开过程结论维度：某方法判为过程正确，而人工参考为过程错误。仅在人工参考明确时统计。"],
    ["failed", "调用失败", "任一方法的状态为 provider_error 或 parse_error。"],
    ["missing", "结果缺失", "任一方法在该样本上没有预测记录。"],
  ];

  const TRI_LABELS = {
    reasoning_correct: ["正确", "错误"],
    plan_code_aligned: ["一致", "不一致"],
    public_process_correct: ["过程成立", "过程不成立"],
    joint_process_correct: ["成立", "不成立"],
    functional_correct: ["正确", "存在问题"],
  };

  const GOLD_TAG = {
    tp: ["与参考一致 · 检出错误", "good"],
    tn: ["与参考一致", "good"],
    fp: ["误报", "bad"],
    fn: ["漏报", "bad"],
    gold_unknown: ["参考未知", "muted"],
    abstained: ["未给出结论", "muted"],
    failed: ["", "muted"],
    missing: ["", "muted"],
  };

  const STATUS_LABEL = {
    ok: ["完成", "good"],
    provider_error: ["调用失败 · provider", "bad"],
    parse_error: ["调用失败 · 解析", "bad"],
    missing: ["结果缺失", "muted"],
  };

  function tri(field, value) {
    if (value === null || value === undefined) return "未给出";
    const [yes, no] = TRI_LABELS[field] || ["是", "否"];
    return value ? yes : no;
  }

  function compareFlags(item) {
    const methods = Object.values(item.methods);
    return {
      disagreement: item.disagreement.any,
      fp: methods.some((m) => m.outcome === "fp"),
      fn: methods.some((m) => m.outcome === "fn"),
      failed: methods.some((m) => m.status === "provider_error" || m.status === "parse_error"),
      missing: methods.some((m) => m.status === "missing"),
    };
  }

  function compareMatches(item, filter) {
    if (filter === "all") return true;
    return compareFlags(item)[filter];
  }

  function usageText(usage) {
    if (!usage || !usage.recorded) return "未记录";
    const seconds = usage.request_seconds === null ? "未记录" : `${usage.request_seconds.toFixed(1)} s`;
    return `${usage.requests} 次 · ${seconds}`;
  }

  function usageTokens(usage) {
    if (!usage || !usage.recorded) return "未记录";
    if (usage.input_tokens === null || usage.output_tokens === null) {
      return `未记录完整（覆盖 ${usage.token_coverage}）`;
    }
    return `输入 ${usage.input_tokens.toLocaleString("zh-CN")} · 输出 ${usage.output_tokens.toLocaleString("zh-CN")}`;
  }

  function bindCitation(detailId, liveId, target, button) {
    const detail = $(detailId);
    const live = $(liveId);
    detail.querySelectorAll(".ref-flash").forEach((node) => node.classList.remove("ref-flash"));
    let selector = null;
    let extra = null;
    if (target.type === "step") selector = `step:${target.step_id}`;
    else if (target.type === "edge_case") selector = `edge:${target.entry_index}`;
    else if (target.type === "code") selector = "field:code";
    else if (target.type === "field") selector = `field:${target.field}`;
    else if (target.type === "requirement") selector = `req:${target.requirement_id}`;
    const node = selector ? detail.querySelector(`[data-anchor="${selector}"]`) : null;
    if (!node) {
      live.textContent = "引用无法核验：未在共用输入中找到对应位置，页面不会猜测位置。";
      const note = el("span", "ref-unverified", "引用无法核验");
      button.insertAdjacentElement("afterend", note);
      setTimeout(() => note.remove(), 4000);
      return;
    }
    const container = node.closest("details");
    if (container) container.open = true;
    node.classList.add("ref-flash");
    if (target.type === "code" && target.code_span) {
      const match = /^L(\d+)(?:-L(\d+))?$/.exec(target.code_span);
      if (match) {
        const start = Number(match[1]);
        const end = Number(match[2] || match[1]);
        for (let line = start; line <= end; line += 1) {
          const row = detail.querySelector(`[data-anchor="code:${line}"]`);
          if (row) row.classList.add("ref-flash");
        }
      }
    }
    node.scrollIntoView({ behavior: "smooth", block: "center" });
    live.textContent = "已定位到共用输入中的引用位置。";
    setTimeout(() => {
      detail.querySelectorAll(".ref-flash").forEach((n) => n.classList.remove("ref-flash"));
    }, 2600);
  }

  function renderCompareHero(data) {
    const exp = data.experiment;
    const chip = $("compare-source");
    if (data.data_kind === "synthetic_ui_test") {
      chip.textContent = "界面测试数据 · 非实验结果";
      chip.classList.add("synthetic");
      $("compare-synthetic-banner").hidden = false;
    } else {
      chip.textContent = `运行身份已校验 · ${exp.run_status}`;
      chip.classList.remove("synthetic");
      $("compare-synthetic-banner").hidden = true;
    }
    const meta = $("compare-meta");
    meta.textContent = "";
    const cells = [
      ["实验名称", exp.name],
      ["实验身份", exp.identity_label || "历史三方法试点 · 2026-09-09"],
      ["运行状态", exp.run_status === "completed" ? "已完成" : exp.run_status],
      ["样本数量", `${exp.item_count} 条 × 3 种方法`],
      ["数据来源", exp.run_dir],
      [
        "人工参考",
        `过程正确 ${exp.label_summary.correct} · 过程错误 ${exp.label_summary.incorrect} · 未知 ${exp.label_summary.unknown}（开发集修订共识）`,
      ],
      [
        "定位参考",
        exp.location_gold.has_reference
          ? `首错步骤金标 ${exp.location_gold.first_step_supported} · 结构化位置金标 ${exp.location_gold.structured_location}`
          : "暂无定位参考（首错步骤与结构化位置金标数均为 0）",
      ],
    ];
    if (exp.requests && exp.requests.dispatched !== null) {
      cells.push([
        "请求与用量",
        `${exp.requests.dispatched} 次请求 · ${exp.requests.request_seconds.toFixed(1)} s · ` +
          `输入 ${exp.requests.input_tokens.toLocaleString("zh-CN")} / 输出 ${exp.requests.output_tokens.toLocaleString("zh-CN")} token（${exp.requests.token_usage === "complete" ? "记录完整" : "记录不完整"}）`,
      ]);
    }
    if (exp.budget) {
      cells.push([
        "预算",
        `已消耗 ${exp.budget.consumed_requests}/${exp.budget.max_requests} 次请求 · 金额未记录（不估算）`,
      ]);
    }
    const grid = el("div", "compare-meta-grid");
    for (const [label, value] of cells) {
      const cell = el("div", "compare-meta-cell");
      cell.appendChild(el("span", null, label));
      cell.appendChild(el("strong", null, value));
      grid.appendChild(cell);
    }
    meta.appendChild(grid);
    if (data.scores) {
      const table = el("div", "cost-table-wrap");
      table.tabIndex = 0;
      const t = el("table", "cost-table");
      const head = el("tr");
      for (const h of ["方法", "完成/总数", "公开过程准确率（全部已知标签）", "判定覆盖率", "误报率（已判定）", "漏报（fn）"]) {
        const th = el("th", null, h);
        th.scope = "col";
        head.appendChild(th);
      }
      t.appendChild(head);
      for (const method of data.experiment.methods) {
        const score = data.scores[method.id];
        const dim = score.public_process;
        const ratio = (r) => (r.value === null ? `${r.numerator}/${r.denominator} · —` : `${r.numerator}/${r.denominator} · ${(r.value * 100).toFixed(1)}%`);
        const tr = el("tr");
        tr.appendChild(el("td", null, method.label));
        tr.appendChild(el("td", null, `${score.coverage.ok}/${data.experiment.item_count}`));
        tr.appendChild(el("td", null, ratio(dim.accuracy_all_known)));
        tr.appendChild(el("td", null, ratio(dim.decision_coverage_known)));
        tr.appendChild(el("td", null, ratio(dim.false_positive_rate_decided)));
        tr.appendChild(el("td", null, String(dim.fn)));
        t.appendChild(tr);
      }
      table.appendChild(t);
      meta.appendChild(table);
      meta.appendChild(el("p", "fineprint", "计分口径与离线 scorer 一致：正类为“过程错误”；accuracy_all_known 的分母包含缺失、失败与弃答；decided-only 指标须与覆盖率一起阅读。人工标签为反馈后的开发集修订共识，非独立测试集金标。"));
    }
    meta.appendChild(resultsOverviewEntry());
    $("compare-notes").textContent = data.notes.join(" ");
  }

  function renderCompareFilters(data) {
    const bar = $("compare-filters");
    bar.textContent = "";
    for (const [id, label, definition] of COMPARE_FILTERS) {
      const count = data.items.filter((item) => compareMatches(item, id)).length;
      const button = el("button", `compare-filter${compareFilter === id ? " active" : ""}`);
      button.dataset.filter = id;
      button.dataset.definition = definition;
      button.setAttribute("aria-pressed", String(compareFilter === id));
      button.appendChild(document.createTextNode(label));
      button.appendChild(el("b", null, String(count)));
      button.addEventListener("click", () => {
        compareFilter = id;
        renderCompareFilters(comparisonData);
        renderCompareList(comparisonData);
        // Re-rendering replaces the button; keep keyboard focus on the filter.
        const replacement = bar.querySelector(`[data-filter="${id}"]`);
        if (replacement) replacement.focus();
      });
      bar.appendChild(button);
    }
    const active = COMPARE_FILTERS.find(([id]) => id === compareFilter);
    $("compare-filter-note").textContent = `筛选定义 · ${active[1]}：${active[2]}`;
  }

  function renderCompareList(data) {
    const list = $("compare-list");
    list.textContent = "";
    const visible = data.items.filter((item) => compareMatches(item, compareFilter));
    if (!visible.length) {
      list.appendChild(el("p", "placeholder", "当前筛选下没有样本。"));
      return;
    }
    for (const item of visible) {
      const flags = compareFlags(item);
      const button = el("button", "case-option compare-option");
      button.dataset.itemId = item.item_id;
      button.appendChild(el("b", null, item.problem.title));
      button.appendChild(el("small", null, item.item_id));
      const badges = el("span", "compare-badges");
      const add = (text, kind) => badges.appendChild(el("i", `flag ${kind}`, text));
      if (flags.disagreement) add("分歧", "warn");
      if (flags.fp) add("误报", "bad");
      if (flags.fn) add("漏报", "bad");
      if (flags.failed) add("失败", "muted");
      if (flags.missing) add("缺失", "muted");
      if (!flags.disagreement && !flags.fp && !flags.fn && !flags.failed && !flags.missing) add("一致", "good");
      button.appendChild(badges);
      button.addEventListener("click", () => selectCompareItem(item.item_id));
      list.appendChild(button);
    }
    if (!visible.some((item) => item.item_id === compareSelection)) {
      compareSelection = visible[0].item_id;
    }
    selectCompareItem(compareSelection);
  }

  function anchoredBlock(anchor, build) {
    const node = build();
    node.setAttribute("data-anchor", anchor);
    return node;
  }

  function renderSharedInput(item, columnWord = "三列") {
    const card = el("section", "shared-input");
    const head = el("div", "shared-input-head");
    head.appendChild(el("span", "chain-index", `共用输入 · ${columnWord}对应同一冻结候选`));
    const idline = el("p", "fineprint");
    idline.appendChild(document.createTextNode(`item_id ${item.item_id} · input SHA-256 ${item.input_sha256.slice(0, 16)}… · 候选代码 SHA-256 ${item.functional_evidence.candidate_code_sha256.slice(0, 16)}…`));
    head.appendChild(idline);
    card.appendChild(head);

    const prob = item.problem;
    const requirement = el("details", "input-section");
    requirement.open = true;
    requirement.appendChild(el("summary", null, "题目需求"));
    const reqBody = anchoredBlock("field:requirement", () => el("div", "input-body"));
    reqBody.appendChild(el("h3", null, prob.title));
    reqBody.appendChild(el("p", null, prob.requirement));
    reqBody.appendChild(el("code", "signature", prob.function_signature));
    const ul = el("ul", "req-list");
    for (const r of prob.requirements) {
      const li = el("li");
      li.setAttribute("data-anchor", `req:${r.requirement_id}`);
      li.appendChild(el("b", null, r.requirement_id));
      li.appendChild(document.createTextNode(r.content));
      ul.appendChild(li);
    }
    reqBody.appendChild(ul);
    requirement.appendChild(reqBody);
    card.appendChild(requirement);

    const trace = item.solution_trace;
    const explanation = el("details", "input-section");
    explanation.appendChild(el("summary", null, "冻结解题说明"));
    const expBody = el("div", "input-body");
    const dl = el("dl", "kv");
    const understanding = el("dd");
    understanding.setAttribute("data-anchor", "field:requirement_understanding");
    understanding.textContent = trace.requirement_understanding;
    dl.appendChild(el("dt", null, "需求理解"));
    dl.appendChild(understanding);
    const design = el("dd");
    design.setAttribute("data-anchor", "field:design_summary");
    design.textContent = trace.design_summary;
    dl.appendChild(el("dt", null, "设计摘要"));
    dl.appendChild(design);
    if (trace.edge_cases_considered.length) {
      dl.appendChild(el("dt", null, "考虑的边界"));
      const edges = el("dd");
      trace.edge_cases_considered.forEach((text, index) => {
        const line = el("div");
        line.setAttribute("data-anchor", `edge:${index}`);
        line.textContent = text;
        edges.appendChild(line);
      });
      dl.appendChild(edges);
    }
    for (const [label, key] of [["声明时间复杂度", "declared_time_complexity"], ["声明空间复杂度", "declared_space_complexity"]]) {
      if (trace[key]) {
        dl.appendChild(el("dt", null, label));
        const dd = el("dd", "mono", trace[key]);
        dd.setAttribute("data-anchor", `field:${key}`);
        dl.appendChild(dd);
      }
    }
    expBody.appendChild(dl);
    explanation.appendChild(expBody);
    card.appendChild(explanation);

    const stepsSection = el("details", "input-section");
    stepsSection.appendChild(el("summary", null, `实施步骤（${trace.implementation_steps.length}）`));
    const steps = el("ul", "step-list");
    for (const step of trace.implementation_steps) {
      const li = el("li");
      li.setAttribute("data-anchor", `step:${step.step_id}`);
      li.appendChild(el("b", null, step.step_id));
      li.appendChild(document.createTextNode(step.content));
      if (step.related_requirements && step.related_requirements.length) {
        li.appendChild(el("span", "refs", `（关联 ${step.related_requirements.join("、")}）`));
      }
      steps.appendChild(li);
    }
    stepsSection.appendChild(steps);
    card.appendChild(stepsSection);

    const codeSection = el("details", "input-section");
    codeSection.appendChild(el("summary", null, `代码（${trace.code.split("\n").length} 行）`));
    const codeBody = el("div", "input-body");
    codeBody.setAttribute("data-anchor", "field:code");
    const pre = el("pre", "code");
    pre.tabIndex = 0;
    trace.code.split("\n").forEach((line, index) => {
      const row = el("span", "code-line");
      row.setAttribute("data-anchor", `code:${index + 1}`);
      row.appendChild(el("span", "lineno", String(index + 1)));
      row.appendChild(document.createTextNode(line || " "));
      pre.appendChild(row);
    });
    codeBody.appendChild(pre);
    codeSection.appendChild(codeBody);
    card.appendChild(codeSection);

    const func = item.functional_evidence;
    const strip = el("div", "functional-strip");
    strip.appendChild(el("span", "chain-index", "功能证据（独立展示 · 官方汇总，非逐用例执行）"));
    const stats = el("div", "agg");
    const addStat = (label, value, kind) => {
      const s = el("span", kind);
      s.appendChild(document.createTextNode(`${label} `));
      s.appendChild(el("b", null, value));
      stats.appendChild(s);
    };
    addStat("基础设施", func.infrastructure_status, func.infrastructure_status === "ok" ? "ok" : "bad");
    addStat("MBPP base", func.base_status, func.base_status === "pass" ? "ok" : "bad");
    addStat("MBPP plus", func.plus_status, func.plus_status === "pass" ? "ok" : "bad");
    addStat(
      "功能结论",
      func.functional_correct === null ? "未知" : func.functional_correct ? "通过" : "未通过",
      func.functional_correct ? "ok" : "bad"
    );
    strip.appendChild(stats);
    strip.appendChild(el("p", "fineprint", "功能失败不等同于过程错误；该证据为官方 base/plus 汇总状态，页面不伪造逐用例执行结果。"));
    card.appendChild(strip);
    return card;
  }

  function goldTag(category) {
    const [text, kind] = GOLD_TAG[category] || ["", "muted"];
    return text ? el("i", `flag ${kind}`, text) : null;
  }

  function renderCompareTable(item, methods) {
    const wrap = el("div", "cost-table-wrap compare-table-wrap");
    wrap.tabIndex = 0;
    const table = el("table", "cost-table compare-table");
    const head = el("tr");
    const corner = el("th", null, "字段");
    corner.scope = "col";
    head.appendChild(corner);
    for (const m of methods) {
      const th = el("th", null, m.label);
      th.scope = "col";
      head.appendChild(th);
    }
    const badgeTh = el("th", null, "对比");
    badgeTh.scope = "col";
    head.appendChild(badgeTh);
    table.appendChild(head);

    const allOk = methods.every((m) => item.methods[m.id].status === "ok");
    const divergent = new Set(item.disagreement.fields);

    const addRow = (field, label, values, options = {}) => {
      const tr = el("tr");
      if (divergent.has(field)) tr.classList.add("divergent");
      const th = el("th", null, label);
      th.scope = "row";
      tr.appendChild(th);
      values.forEach((value, index) => {
        const td = el("td");
        if (typeof value === "string") td.textContent = value;
        else td.appendChild(value);
        if (options.gold) {
          const category = item.methods[methods[index].id].gold_comparison[field];
          const tag = goldTag(category);
          if (tag) {
            td.appendChild(document.createTextNode(" "));
            td.appendChild(tag);
          }
        }
        tr.appendChild(td);
      });
      const badge = el("td", "compare-badge-cell");
      if (!options.comparable) {
        badge.appendChild(el("i", "flag muted", "—"));
      } else if (!allOk) {
        badge.appendChild(el("i", "flag muted", "无法全比"));
      } else if (divergent.has(field)) {
        badge.appendChild(el("i", "flag warn", "分歧"));
      } else {
        badge.appendChild(el("i", "flag good", "一致"));
      }
      tr.appendChild(badge);
      table.appendChild(tr);
    };

    addRow("status", "状态", methods.map((m) => {
      const [text, kind] = STATUS_LABEL[item.methods[m.id].status];
      const span = el("span");
      span.appendChild(el("i", `flag ${kind}`, text));
      return span;
    }));

    const dims = [
      ["reasoning_correct", "说明正确性"],
      ["plan_code_aligned", "计划—代码对齐"],
      ["public_process_correct", "公开过程结论"],
    ];
    for (const [field, label] of dims) {
      addRow(field, label, methods.map((m) => {
        const a = item.methods[m.id].assessment;
        return a ? tri(field, a[field]) : "—";
      }), { gold: true, comparable: true });
    }
    addRow("joint_process_correct", "联合 process_correct", methods.map((m) => {
      const a = item.methods[m.id].assessment;
      return a ? tri("joint_process_correct", a.joint_process_correct) : "—";
    }));
    addRow("functional_correct", "功能结论（独立）", methods.map((m) => {
      const a = item.methods[m.id].assessment;
      if (!a) return "—";
      return a.functional_correct === null ? "不适用 · 该方法不接入功能证据" : tri("functional_correct", a.functional_correct);
    }));
    addRow("first_faulty_layer", "首错层级", methods.map((m) => {
      const a = item.methods[m.id].assessment;
      return a ? a.first_faulty_layer || "—" : "—";
    }), { comparable: true });
    addRow("first_faulty_step", "首错步骤", methods.map((m) => {
      const a = item.methods[m.id].assessment;
      return a ? a.first_faulty_step || "—" : "—";
    }), { comparable: true });
    addRow("error_type", "错误类型", methods.map((m) => {
      const a = item.methods[m.id].assessment;
      return a ? a.error_type || "—" : "—";
    }), { comparable: true });
    addRow("usage_requests", "请求次数 / 耗时", methods.map((m) => usageText(item.methods[m.id].usage)));
    addRow("usage_tokens", "Token 用量", methods.map((m) => usageTokens(item.methods[m.id].usage)));
    wrap.appendChild(table);
    return wrap;
  }

  function renderMethodCards(item, methods) {
    const grid = el("div", "method-cards");
    for (const m of methods) {
      const view = item.methods[m.id];
      const card = el("section", "method-card");
      const head = el("div", "method-card-head");
      head.appendChild(el("h3", null, m.label));
      const [statusText, statusKind] = STATUS_LABEL[view.status];
      head.appendChild(el("i", `flag ${statusKind}`, statusText));
      card.appendChild(head);
      if (!view.assessment) {
        card.appendChild(el("p", "placeholder", view.status === "missing" ? "运行中没有该方法的预测记录；不以任何内容补齐。" : "该方法调用失败，没有可用判断；失败不计入方法分歧。"));
      } else {
        const a = view.assessment;
        card.appendChild(el("p", "explanation", a.explanation));
        if (a.references && a.references.length) {
          const refs = el("div", "ref-list");
          refs.appendChild(el("span", "fineprint", "原文引用（点击定位到共用输入）："));
          for (const ref of a.references) {
            const button = el("button", "ref-button", ref.label);
            button.addEventListener("click", () => bindCitation("compare-detail", "compare-live", ref.target, button));
            refs.appendChild(button);
          }
          card.appendChild(refs);
          card.appendChild(el("p", "fineprint", "引用出现在原文只证明绑定成功，不证明错误判断成立。"));
        }
        const mini = el("dl", "kv method-mini");
        const addKv = (key, value) => {
          mini.appendChild(el("dt", null, key));
          mini.appendChild(el("dd", "mono", value));
        };
        if (a.first_faulty_location) {
          const loc = a.first_faulty_location;
          addKv("首错位置", `${loc.source_field}${loc.step_id ? ` · ${loc.step_id}` : ""}${loc.code_span ? ` · ${loc.code_span}` : ""}`);
          addKv("原文引文", loc.quote.length > 120 ? `${loc.quote.slice(0, 120)}…` : loc.quote);
        }
        if (a.code_span) addKv("code_span", a.code_span);
        if (a.affected_steps.length) addKv("受影响步骤", a.affected_steps.join(", "));
        if (a.secondary_error_types.length) addKv("次要错误类型", a.secondary_error_types.join(", "));
        if (a.confidence !== null && a.confidence !== undefined) addKv("confidence", String(a.confidence));
        if (mini.children.length) card.appendChild(mini);
      }
      grid.appendChild(card);
    }
    return grid;
  }

  function renderHumanReference(item, experiment) {
    const ref = item.human_reference;
    const details = el("details", "human-ref");
    const summary = el("summary", null, "人工参考（默认折叠 · 开发集修订共识，仅用于展示与对照，不进入模型请求）");
    details.appendChild(summary);
    const body = el("div", "human-ref-body");
    const dl = el("dl", "kv");
    const triText = (v) => (v === null ? "未知" : v ? "正确" : "错误");
    const addKv = (key, value) => {
      dl.appendChild(el("dt", null, key));
      dl.appendChild(el("dd", null, value));
    };
    addKv("说明正确性", triText(ref.reasoning_correct));
    addKv("计划—代码对齐", triText(ref.plan_code_aligned));
    addKv("公开过程结论", ref.process_correct === null ? "未知" : ref.process_correct ? "过程成立" : "过程不成立");
    if (!experiment.location_gold.has_reference) {
      addKv("首错位置参考", "暂无定位参考（金标数为 0；不标记定位正确，null/null 不算命中）");
    } else {
      addKv("首错层级", ref.first_faulty_layer || "—");
      addKv("首错步骤", ref.first_faulty_step || "—");
    }
    addKv("错误类型", ref.error_type || "—");
    addKv("定位状态", ref.localization_status);
    body.appendChild(dl);
    if (ref.evidence && ref.evidence.length) {
      const list = el("ul", "step-list");
      for (const entry of ref.evidence) {
        const li = el("li");
        const where = [entry.step_id, entry.code_span, entry.requirement_id].filter(Boolean).join(" · ");
        if (where) li.appendChild(el("b", null, where));
        li.appendChild(document.createTextNode(entry.description));
        list.appendChild(li);
      }
      body.appendChild(list);
    }
    body.appendChild(el("p", "explanation", ref.rationale));
    body.appendChild(el("p", "boundary-note", "以上是反馈后的开发集修订共识标签，不是独立留出测试集金标；标注者独立性未核实。未知标签保持未知，不转为正确或错误。"));
    details.appendChild(body);
    return details;
  }

  function renderCompareDetail(item) {
    const root = $("compare-detail");
    root.textContent = "";
    const methods = comparisonData.experiment.methods;
    const head = el("div", "case-detail-head");
    const heading = el("div");
    heading.appendChild(el("span", "eyebrow", item.item_id));
    heading.appendChild(el("h2", null, item.problem.title));
    head.appendChild(heading);
    if (item.disagreement.any) {
      head.appendChild(el("span", "case-verdict bad", `方法分歧 · ${item.disagreement.fields.length} 个字段`));
    } else {
      const allOk = methods.every((m) => item.methods[m.id].status === "ok");
      head.appendChild(el("span", `case-verdict ${allOk ? "good" : ""}`, allOk ? "三方法一致" : "含失败/缺失 · 不判分歧"));
    }
    root.appendChild(head);
    root.appendChild(renderSharedInput(item));
    root.appendChild(el("h3", "compare-section-title", "三列方法判断对照"));
    root.appendChild(renderCompareTable(item, methods));
    root.appendChild(renderMethodCards(item, methods));
    root.appendChild(renderHumanReference(item, comparisonData.experiment));
  }

  function selectCompareItem(itemId) {
    if (!comparisonData) return;
    const item = comparisonData.items.find((entry) => entry.item_id === itemId);
    if (!item) return;
    compareSelection = itemId;
    document.querySelectorAll(".compare-option").forEach((button) => {
      const active = button.dataset.itemId === itemId;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    renderCompareDetail(item);
  }

  function renderCompareEmpty(message) {
    $("compare-source").textContent = "实验结果尚未就绪";
    $("compare-synthetic-banner").hidden = true;
    $("compare-meta").textContent = "";
    $("compare-filters").textContent = "";
    $("compare-filter-note").textContent = "";
    $("compare-list").textContent = "";
    const root = $("compare-detail");
    root.textContent = "";
    const box = el("div", "error-box compare-empty");
    box.appendChild(el("b", null, "实验结果尚未就绪"));
    box.appendChild(document.createTextNode(`：${message}`));
    box.appendChild(el("small", null, "需要：配置的运行目录内含 run-report.json，且其哈希绑定的 predictions.jsonl、requests.jsonl、run-config.json 完整一致，运行身份与 pilot 配置及逐条输入哈希匹配。"));
    root.appendChild(box);
    const demo = el("button", "compact-button", "载入界面测试数据（合成 · 仅验证页面状态）");
    demo.addEventListener("click", () => loadComparison(true));
    root.appendChild(demo);
  }

  async function loadComparison(synthetic = false) {
    if (comparisonRequested && !synthetic) return;
    comparisonRequested = true;
    try {
      const resp = await fetch(synthetic ? "/api/method-comparison?fixture=synthetic" : "/api/method-comparison");
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || "实验结果不可用");
      comparisonData = data;
      compareSelection = null;
      renderCompareHero(data);
      renderCompareFilters(data);
      renderCompareList(data);
    } catch (exc) {
      if (synthetic) {
        renderCompareEmpty(`合成界面数据也无法载入：${exc.message}`);
        return;
      }
      renderCompareEmpty(exc.message || "实验结果尚未就绪。");
    }
  }

  // -------------------------------------------------- evidence ablation 2x2
  // Read-only view of the pinned ablation run: one frozen candidate judged
  // under four evidence conditions (A/B/C/D).  Separate experiment from the
  // three-method page; condition ids are never renamed to Direct/Structured/
  // Full.  textContent-only rendering throughout.

  let ablationData = null;
  let ablationRequested = false;
  let ablationSelection = null;
  let ablationFilter = "all";
  let ablationScoreView = "judge_raw"; // or "rule_merged"

  const ABLATION_FILTERS = [
    ["all", "全部样本", "不过滤：展示运行中的全部样本，避免只展示系统表现好的案例。"],
    ["disagreement", "条件分歧", "四个条件中已完成判断（status=ok）的条件在任一可比字段上取值不同（按当前视图）。调用失败、结果缺失或弃答不算分歧。"],
    ["fp", "相对标签误报", "公开过程结论维度：某条件判为过程错误，而当前人工参考为过程正确。仅在人工参考明确时统计；标签争议样本见重点案例说明。"],
    ["fn", "相对标签漏报", "公开过程结论维度：某条件判为过程正确，而当前人工参考为过程错误。仅在人工参考明确时统计；标签争议样本见重点案例说明。"],
    ["unknown", "参考未知", "人工过程参考为未知的样本；未知保持未知，不转为正确或错误。"],
    ["failed", "调用失败", "任一条件的状态为 provider_error 或 parse_error。"],
    ["missing", "结果缺失", "任一条件在该样本上没有预测记录。"],
  ];

  const ABLATION_VIEW_LABELS = { judge_raw: "Judge 原始输出", rule_merged: "规则合并后" };

  function abViewKey() {
    return ablationScoreView === "judge_raw" ? "judge" : "merged";
  }

  function abFlags(item) {
    const conditions = Object.values(item.conditions);
    const outcomeKey = ablationScoreView === "judge_raw" ? "outcome_judge" : "outcome_merged";
    return {
      disagreement: item.disagreement[abViewKey()].any,
      fp: conditions.some((c) => c[outcomeKey] === "fp"),
      fn: conditions.some((c) => c[outcomeKey] === "fn"),
      unknown: item.human_reference.process_correct === null,
      failed: conditions.some((c) => c.status === "provider_error" || c.status === "parse_error"),
      missing: conditions.some((c) => c.status === "missing"),
    };
  }

  function abMatches(item, filter) {
    if (filter === "all") return true;
    return abFlags(item)[filter];
  }

  function renderAblationHero(data) {
    const exp = data.experiment;
    const chip = $("ablation-source");
    if (data.data_kind === "synthetic_ui_test") {
      chip.textContent = "界面测试数据 · 非实验结果";
      chip.classList.add("synthetic");
      $("ablation-synthetic-banner").hidden = false;
    } else {
      chip.textContent = `运行身份与计分已校验 · ${exp.run_status}`;
      chip.classList.remove("synthetic");
      $("ablation-synthetic-banner").hidden = true;
    }
    const meta = $("ablation-meta");
    meta.textContent = "";

    const intro = el("p", "fineprint",
      "实验定义：四个条件共用同一核心任务说明、同一错误分类体系、同一输出 schema、同一 Judge 模型与生成参数、同一批冻结候选；" +
      "唯一差异是进入 Judge 的证据。“不提供”指该证据不进入 Judge 输入；AST 静态分析与确定性规则层对全部四个条件都离线运行，用于分离规则贡献。");
    meta.appendChild(intro);

    // Evidence matrix: condition × evidence kind.
    const matrixWrap = el("div", "cost-table-wrap");
    matrixWrap.tabIndex = 0;
    const matrix = el("table", "cost-table");
    const mhead = el("tr");
    for (const h of ["条件", "静态证据进入 Judge", "官方功能汇总进入 Judge", "说明"]) {
      const th = el("th", null, h);
      th.scope = "col";
      mhead.appendChild(th);
    }
    matrix.appendChild(mhead);
    for (const condition of exp.conditions) {
      const tr = el("tr");
      tr.appendChild(el("td", null, condition.label));
      const staticTd = el("td");
      staticTd.appendChild(el("i", `flag ${condition.static_evidence ? "warn" : "muted"}`, condition.static_evidence ? "提供" : "不提供"));
      tr.appendChild(staticTd);
      const funcTd = el("td");
      funcTd.appendChild(el("i", `flag ${condition.functional_evidence ? "warn" : "muted"}`, condition.functional_evidence ? "提供" : "不提供"));
      tr.appendChild(funcTd);
      tr.appendChild(el("td", null, condition.description));
      matrix.appendChild(tr);
    }
    matrixWrap.appendChild(matrix);
    meta.appendChild(matrixWrap);

    const cells = [
      ["实验名称", exp.name],
      ["实验身份", exp.identity_label || "历史单轮消融 · 2026-09-09 · 原方案与原标签"],
      ["运行状态", exp.run_status === "completed" ? "已完成" : exp.run_status],
      ["样本数量", `${exp.item_count} 条 × 4 个条件（单轮 rep1 · 每条件每题一次判断）`],
      ["数据来源", `${exp.run_dir} · 计分 ${exp.scores_file}`],
      [
        "人工参考",
        `过程正确 ${exp.label_summary.correct} · 过程错误 ${exp.label_summary.incorrect} · 未知 ${exp.label_summary.unknown}（开发集修订共识）`,
      ],
      [
        "定位参考",
        exp.location_gold.has_reference
          ? `首错步骤金标 ${exp.location_gold.first_step_supported} · 结构化位置金标 ${exp.location_gold.structured_location}`
          : "暂无定位参考（首错步骤与结构化位置金标数均为 0）",
      ],
    ];
    if (exp.plan_sha256) {
      cells.push(["预注册方案", `SHA-256 ${exp.plan_sha256.slice(0, 16)}… · 种子 ${exp.seed}`]);
    }
    if (exp.requests && exp.requests.dispatched !== null && exp.requests.dispatched !== undefined) {
      const seconds = exp.requests.request_seconds === null || exp.requests.request_seconds === undefined
        ? "未记录"
        : `${exp.requests.request_seconds.toFixed(1)} s`;
      const tokens = exp.requests.input_tokens === null || exp.requests.input_tokens === undefined
        ? "token 未记录"
        : `输入 ${exp.requests.input_tokens.toLocaleString("zh-CN")} / 输出 ${exp.requests.output_tokens.toLocaleString("zh-CN")} token（${exp.requests.token_usage === "complete" ? "记录完整" : "记录不完整"}）`;
      cells.push(["请求与用量", `${exp.requests.dispatched} 次请求 · ${seconds} · ${tokens}`]);
    }
    if (exp.budget) {
      cells.push([
        "预算与成本",
        `已消耗 ${exp.budget.consumed_requests}/${exp.budget.max_requests} 次请求 · 金额未记录（不估算、不填零，成本保持未知）`,
      ]);
    }
    const grid = el("div", "compare-meta-grid");
    for (const [label, value] of cells) {
      const cell = el("div", "compare-meta-cell");
      cell.appendChild(el("span", null, label));
      cell.appendChild(el("strong", null, value));
      grid.appendChild(cell);
    }
    meta.appendChild(grid);

    if (data.scores) meta.appendChild(renderAblationScores(data));
    if (data.repetition) meta.appendChild(renderRepetitionBlock(data.repetition));
    meta.appendChild(resultsOverviewEntry());
    $("ablation-notes").textContent = data.notes.join(" ");
  }

  // Five-repetition summary: rendered from the server-verified tally payload.
  // It stays a separate block -- never rows inside the single-round table and
  // never part of the paired transition counts.
  function renderRepetitionBlock(rep) {
    const box = el("section", "repetition-block");
    const head = el("div", "repetition-head");
    head.appendChild(el("span", "eyebrow", "FIVE-REPETITION TALLY · 五轮重复"));
    head.appendChild(el("h3", null, rep.title));
    box.appendChild(head);
    const ul = el("ul", "cv-lines");
    for (const line of rep.lines) ul.appendChild(el("li", null, line));
    box.appendChild(ul);
    if (rep.review_doc) {
      box.appendChild(el("p", "fineprint", `重复报告：${rep.review_doc}（仓库内文档，本页不抓取）。上方表格仍是原始单轮结果，未混入任何重复轮次。`));
    }
    return box;
  }

  function renderAblationScores(data) {
    const box = el("section", "ablation-scores");
    const head = el("div", "ablation-scores-head");
    head.appendChild(el("h3", null, "公开过程口径计分（与离线 scorer 一致的已核验数字）"));
    const toggle = el("div", "view-toggle");
    toggle.setAttribute("role", "group");
    toggle.setAttribute("aria-label", "判断视图切换");
    for (const view of ["judge_raw", "rule_merged"]) {
      const button = el("button", `compare-filter${ablationScoreView === view ? " active" : ""}`, ABLATION_VIEW_LABELS[view]);
      button.setAttribute("aria-pressed", String(ablationScoreView === view));
      button.addEventListener("click", () => {
        if (ablationScoreView === view) return;
        ablationScoreView = view;
        renderAblationHero(ablationData);
        renderAblationFilters(ablationData);
        renderAblationList(ablationData);
        const replacement = $("ablation-meta").querySelector(".view-toggle button[aria-pressed='true']");
        if (replacement) replacement.focus();
      });
      toggle.appendChild(button);
    }
    head.appendChild(toggle);
    box.appendChild(head);

    const viewScores = data.scores.views[ablationScoreView];
    const table = el("div", "cost-table-wrap");
    table.tabIndex = 0;
    const t = el("table", "cost-table");
    const headRow = el("tr");
    for (const h of ["条件", "完成/总数", "已知标签准确率", "tp", "fp", "tn", "fn", "误报率（已判定）", "错误召回（已判定）"]) {
      const th = el("th", null, h);
      th.scope = "col";
      headRow.appendChild(th);
    }
    t.appendChild(headRow);
    const ratio = (r) => (r.value === null || r.value === undefined ? `${r.numerator}/${r.denominator} · —` : `${r.numerator}/${r.denominator} · ${(r.value * 100).toFixed(1)}%`);
    for (const condition of data.experiment.conditions) {
      const score = viewScores[condition.id];
      const dim = score.public_process;
      const tr = el("tr");
      tr.appendChild(el("td", null, condition.label));
      tr.appendChild(el("td", null, `${score.coverage.ok}/${data.experiment.item_count}`));
      tr.appendChild(el("td", null, ratio(dim.accuracy_all_known)));
      for (const key of ["tp", "fp", "tn", "fn"]) tr.appendChild(el("td", null, String(dim[key])));
      tr.appendChild(el("td", null, ratio(dim.false_positive_rate_decided)));
      tr.appendChild(el("td", null, ratio(dim.error_recall_decided)));
      t.appendChild(tr);
    }
    table.appendChild(t);
    box.appendChild(table);

    // Paired comparisons (pre-registered).
    const paired = el("div", "ablation-paired");
    paired.appendChild(el("h4", null, "预注册成对比较（按当前视图计分文件口径为规则合并视图；本轮两视图一致）"));
    const ul = el("ul", "step-list");
    for (const pc of data.scores.paired_comparisons) {
      const li = el("li");
      const changed = Object.entries(pc.changed_items);
      li.appendChild(el("b", null, `${shortCondition(pc.treatment)} − ${shortCondition(pc.baseline)}`));
      li.appendChild(document.createTextNode(`：${Object.entries(pc.transitions).map(([k, v]) => `${k} ×${v}`).join("，")}`));
      if (changed.length) {
        li.appendChild(document.createTextNode(`；变化样本：${changed.map(([id, tr]) => `${id}（${tr}）`).join("、")}`));
      } else {
        li.appendChild(document.createTextNode("；本轮无判断变化。"));
      }
      ul.appendChild(li);
    }
    paired.appendChild(ul);
    box.appendChild(paired);

    // Rule effects summary.
    const ruleLine = el("p", "fineprint");
    const firedSummary = data.experiment.conditions
      .map((condition) => {
        const effect = data.scores.rule_effects[condition.id];
        const fired = Object.values(effect.rules_fired).reduce((a, b) => a + b, 0);
        return `${shortCondition(condition.id)} 触发 ${fired} 次 / 改变判断 ${effect.changed_items.length} 条`;
      })
      .join("；");
    ruleLine.textContent = data.scores.views_identical
      ? `确定性规则层：${firedSummary}。本轮规则未触发，judge_raw 与 rule_merged 两个视图完全一致，未观察到规则合并贡献；这不外推为规则在其他任务上无效。`
      : `确定性规则层：${firedSummary}。切换上方视图可比较规则合并前后的判断。`;
    box.appendChild(ruleLine);
    box.appendChild(el("p", "fineprint", "计分口径：正类为“过程错误”；accuracy_all_known 的分母为全部已知标签（含缺失、失败与弃答）；decided-only 指标须与覆盖率一起阅读。当前表格仍是原始单轮（rep1）结果；五轮重复另行汇总，重复结果未观察到稳定的 D 优势，不能从 rep1 推断证据条件的因果贡献。"));
    return box;
  }

  function shortCondition(conditionId) {
    const labels = { ablation_a: "A", ablation_b: "B", ablation_c: "C", ablation_d: "D" };
    return labels[conditionId] || conditionId;
  }

  function renderAblationFilters(data) {
    const bar = $("ablation-filters");
    bar.textContent = "";
    for (const [id, label, definition] of ABLATION_FILTERS) {
      const count = data.items.filter((item) => abMatches(item, id)).length;
      const button = el("button", `compare-filter${ablationFilter === id ? " active" : ""}`);
      button.dataset.filter = id;
      button.dataset.definition = definition;
      button.setAttribute("aria-pressed", String(ablationFilter === id));
      button.appendChild(document.createTextNode(label));
      button.appendChild(el("b", null, String(count)));
      button.addEventListener("click", () => {
        ablationFilter = id;
        renderAblationFilters(ablationData);
        renderAblationList(ablationData);
        const replacement = bar.querySelector(`[data-filter="${id}"]`);
        if (replacement) replacement.focus();
      });
      bar.appendChild(button);
    }
    const active = ABLATION_FILTERS.find(([id]) => id === ablationFilter);
    $("ablation-filter-note").textContent = `筛选定义 · ${active[1]}：${active[2]}`;

    const cases = $("ablation-cases");
    cases.textContent = "";
    if (data.focus_cases && data.focus_cases.length) {
      cases.appendChild(el("span", "fineprint", "重点案例："));
      for (const focus of data.focus_cases) {
        const button = el("button", "compact-button", focus.label);
        button.addEventListener("click", () => {
          ablationFilter = "all";
          renderAblationFilters(ablationData);
          renderAblationList(ablationData);
          selectAblationItem(focus.item_id);
          const option = $("ablation-list").querySelector(`[data-item-id="${focus.item_id}"]`);
          if (option) option.focus();
        });
        cases.appendChild(button);
      }
    }
  }

  function renderAblationList(data) {
    const list = $("ablation-list");
    list.textContent = "";
    const visible = data.items.filter((item) => abMatches(item, ablationFilter));
    if (!visible.length) {
      list.appendChild(el("p", "placeholder", "当前筛选下没有样本。"));
      return;
    }
    for (const item of visible) {
      const flags = abFlags(item);
      const button = el("button", "case-option compare-option");
      button.dataset.itemId = item.item_id;
      button.appendChild(el("b", null, item.problem.title));
      button.appendChild(el("small", null, item.item_id));
      const badges = el("span", "compare-badges");
      const add = (text, kind) => badges.appendChild(el("i", `flag ${kind}`, text));
      if (item.case_note) add("重点案例", "warn");
      if (flags.disagreement) add("条件分歧", "warn");
      if (flags.fp) add("误报", "bad");
      if (flags.fn) add("漏报", "bad");
      if (flags.unknown) add("参考未知", "muted");
      if (flags.failed) add("失败", "muted");
      if (flags.missing) add("缺失", "muted");
      if (!item.case_note && !flags.disagreement && !flags.fp && !flags.fn && !flags.failed && !flags.missing && !flags.unknown) add("一致", "good");
      button.appendChild(badges);
      button.addEventListener("click", () => selectAblationItem(item.item_id));
      list.appendChild(button);
    }
    if (!visible.some((item) => item.item_id === ablationSelection)) {
      ablationSelection = visible[0].item_id;
    }
    selectAblationItem(ablationSelection);
  }

  function renderCaseNote(note) {
    const box = el("section", "case-note");
    box.appendChild(el("h3", null, note.title));
    const ul = el("ul", "step-list");
    for (const point of note.points) {
      const li = el("li");
      li.textContent = point;
      ul.appendChild(li);
    }
    box.appendChild(ul);
    if (note.historical_reference) {
      const hist = el("div", "case-note-history");
      hist.appendChild(el("b", null, "历史参考（独立实验设置，不并入本轮结论）"));
      hist.appendChild(el("p", null, note.historical_reference));
      box.appendChild(hist);
    }
    if (note.review_doc) {
      box.appendChild(el("p", "fineprint", `审核依据：${note.review_doc}（仓库内文档，本页不抓取）。`));
    }
    return box;
  }

  function renderAblationTable(item, conditions) {
    const viewKey = abViewKey();
    const outcomeKey = ablationScoreView === "judge_raw" ? "outcome_judge" : "outcome_merged";
    const wrap = el("div", "cost-table-wrap compare-table-wrap");
    wrap.tabIndex = 0;
    const table = el("table", "cost-table compare-table");
    const head = el("tr");
    const corner = el("th", null, "字段");
    corner.scope = "col";
    head.appendChild(corner);
    for (const c of conditions) {
      const th = el("th", null, c.label);
      th.scope = "col";
      head.appendChild(th);
    }
    const badgeTh = el("th", null, "对比");
    badgeTh.scope = "col";
    head.appendChild(badgeTh);
    table.appendChild(head);

    const allOk = conditions.every((c) => item.conditions[c.id].status === "ok");
    const divergent = new Set(item.disagreement[viewKey].fields);

    const addRow = (field, label, values, options = {}) => {
      const tr = el("tr");
      if (divergent.has(field)) tr.classList.add("divergent");
      const th = el("th", null, label);
      th.scope = "row";
      tr.appendChild(th);
      values.forEach((value, index) => {
        const td = el("td");
        if (typeof value === "string") td.textContent = value;
        else td.appendChild(value);
        if (options.gold) {
          const tag = goldTag(item.conditions[conditions[index].id][outcomeKey]);
          if (tag) {
            td.appendChild(document.createTextNode(" "));
            td.appendChild(tag);
          }
        }
        tr.appendChild(td);
      });
      const badge = el("td", "compare-badge-cell");
      if (!options.comparable) {
        badge.appendChild(el("i", "flag muted", "—"));
      } else if (!allOk) {
        badge.appendChild(el("i", "flag muted", "无法全比"));
      } else if (divergent.has(field)) {
        badge.appendChild(el("i", "flag warn", "分歧"));
      } else {
        badge.appendChild(el("i", "flag good", "一致"));
      }
      tr.appendChild(badge);
      table.appendChild(tr);
    };

    addRow("status", "状态", conditions.map((c) => {
      const [text, kind] = STATUS_LABEL[item.conditions[c.id].status];
      const span = el("span");
      span.appendChild(el("i", `flag ${kind}`, text));
      return span;
    }));
    addRow("evidence", "可见证据", conditions.map((c) => {
      const parts = [];
      if (c.static_evidence) parts.push("静态");
      if (c.functional_evidence) parts.push("功能汇总");
      return parts.length ? parts.join("+") : "仅公开材料";
    }));

    const viewAssessment = (c) => item.conditions[c.id][viewKey];
    const dims = [
      ["reasoning_correct", "说明正确性"],
      ["plan_code_aligned", "计划—代码对齐"],
    ];
    for (const [field, label] of dims) {
      addRow(field, label, conditions.map((c) => {
        const a = viewAssessment(c);
        return a ? tri(field, a[field]) : "—";
      }), { comparable: true });
    }
    addRow("public_process_correct", `公开过程结论（${ABLATION_VIEW_LABELS[ablationScoreView]}）`, conditions.map((c) => {
      const a = viewAssessment(c);
      return a ? tri("public_process_correct", a.public_process_correct) : "—";
    }), { gold: true, comparable: true });
    addRow("first_faulty_layer", "首错层级", conditions.map((c) => {
      const a = viewAssessment(c);
      return a ? a.first_faulty_layer || "—" : "—";
    }), { comparable: true });
    addRow("first_faulty_step", "首错步骤", conditions.map((c) => {
      const a = viewAssessment(c);
      return a ? a.first_faulty_step || "—" : "—";
    }), { comparable: true });
    addRow("error_type", "错误类型", conditions.map((c) => {
      const a = viewAssessment(c);
      return a ? a.error_type || "—" : "—";
    }), { comparable: true });
    addRow("rules", "规则触发 / 改变判断", conditions.map((c) => {
      const view = item.conditions[c.id];
      if (view.status !== "ok") return "—";
      const fired = (view.rules || []).filter((r) => r.fired).length;
      const changed = view.rule_changed === null || view.rule_changed === undefined ? "—" : view.rule_changed ? "改变" : "未改变";
      return `${fired} 条触发 · ${changed}`;
    }));
    addRow("usage_requests", "请求次数 / 耗时", conditions.map((c) => usageText(item.conditions[c.id].usage)));
    addRow("usage_tokens", "Token 用量", conditions.map((c) => usageTokens(item.conditions[c.id].usage)));
    wrap.appendChild(table);
    return wrap;
  }

  function renderConditionCards(item, conditions) {
    const grid = el("div", "method-cards");
    for (const c of conditions) {
      const view = item.conditions[c.id];
      const card = el("section", "method-card");
      const head = el("div", "method-card-head");
      head.appendChild(el("h3", null, c.label));
      const [statusText, statusKind] = STATUS_LABEL[view.status];
      head.appendChild(el("i", `flag ${statusKind}`, statusText));
      card.appendChild(head);
      if (!view.judge) {
        card.appendChild(el("p", "placeholder", view.status === "missing"
          ? "运行中没有该条件的预测记录；不以任何内容补齐。"
          : "该条件调用失败，没有可用判断；失败不计入条件分歧。"));
      } else {
        card.appendChild(el("p", "fineprint", `Judge 原始解释（模型输出文字，未执行验证）：`));
        card.appendChild(el("p", "explanation", view.judge.explanation));
        if (view.rule_changed) {
          card.appendChild(el("p", "fineprint", `规则合并改变了判断（合并来源：${view.combination_source}）；合并后解释：`));
          card.appendChild(el("p", "explanation", view.merged.explanation));
        }
        if (view.rules && view.rules.length) {
          const fired = view.rules.filter((r) => r.fired);
          const ruleBox = el("div", "rule-box");
          if (fired.length) {
            ruleBox.appendChild(el("span", "fineprint", "触发的确定性规则："));
            const ul = el("ul", "step-list");
            for (const rule of fired) {
              const li = el("li");
              li.appendChild(el("b", null, rule.rule_id));
              li.appendChild(document.createTextNode(`：${rule.basis || "（无依据文本）"}`));
              ul.appendChild(li);
            }
            ruleBox.appendChild(ul);
          } else {
            ruleBox.appendChild(el("span", "fineprint", "本轮该条件确定性规则均未触发。"));
          }
          card.appendChild(ruleBox);
        }
        const a = view[abViewKey()];
        if (a.references && a.references.length) {
          const refs = el("div", "ref-list");
          refs.appendChild(el("span", "fineprint", "原文引用（点击定位到共用输入）："));
          for (const ref of a.references) {
            const button = el("button", "ref-button", ref.label);
            button.addEventListener("click", () => bindCitation("ablation-detail", "ablation-live", ref.target, button));
            refs.appendChild(button);
          }
          card.appendChild(refs);
          card.appendChild(el("p", "fineprint", "引用出现在原文只证明绑定成功，不证明错误判断成立。"));
        }
        const mini = el("dl", "kv method-mini");
        const addKv = (key, value) => {
          mini.appendChild(el("dt", null, key));
          mini.appendChild(el("dd", "mono", value));
        };
        if (a.first_faulty_location) {
          const loc = a.first_faulty_location;
          addKv("首错位置", `${loc.source_field}${loc.step_id ? ` · ${loc.step_id}` : ""}${loc.code_span ? ` · ${loc.code_span}` : ""}`);
          addKv("原文引文", loc.quote.length > 120 ? `${loc.quote.slice(0, 120)}…` : loc.quote);
        }
        if (a.code_span) addKv("code_span", a.code_span);
        if (a.affected_steps.length) addKv("受影响步骤", a.affected_steps.join(", "));
        if (a.secondary_error_types.length) addKv("次要错误类型", a.secondary_error_types.join(", "));
        if (a.confidence !== null && a.confidence !== undefined) addKv("confidence", String(a.confidence));
        if (mini.children.length) card.appendChild(mini);
      }
      grid.appendChild(card);
    }
    return grid;
  }

  function renderAblationDetail(item) {
    const root = $("ablation-detail");
    root.textContent = "";
    const conditions = ablationData.experiment.conditions;
    const head = el("div", "case-detail-head");
    const heading = el("div");
    heading.appendChild(el("span", "eyebrow", item.item_id));
    heading.appendChild(el("h2", null, item.problem.title));
    head.appendChild(heading);
    const disagreement = item.disagreement[abViewKey()];
    if (disagreement.any) {
      head.appendChild(el("span", "case-verdict bad", `条件分歧（${ABLATION_VIEW_LABELS[ablationScoreView]}）· ${disagreement.fields.length} 个字段`));
    } else {
      const allOk = conditions.every((c) => item.conditions[c.id].status === "ok");
      head.appendChild(el("span", `case-verdict ${allOk ? "good" : ""}`, allOk ? "四条件一致" : "含失败/缺失 · 不判分歧"));
    }
    root.appendChild(head);
    if (item.case_note) root.appendChild(renderCaseNote(item.case_note));
    root.appendChild(renderSharedInput(item, "四列"));
    root.appendChild(el("h3", "compare-section-title", `四列条件判断对照（当前视图：${ABLATION_VIEW_LABELS[ablationScoreView]}）`));
    root.appendChild(renderAblationTable(item, conditions));
    root.appendChild(renderConditionCards(item, conditions));
    root.appendChild(renderHumanReference(item, ablationData.experiment));
  }

  function selectAblationItem(itemId) {
    if (!ablationData) return;
    const item = ablationData.items.find((entry) => entry.item_id === itemId);
    if (!item) return;
    ablationSelection = itemId;
    document.querySelectorAll("#ablation-list .compare-option").forEach((button) => {
      const active = button.dataset.itemId === itemId;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    renderAblationDetail(item);
  }

  function renderAblationEmpty(message) {
    $("ablation-source").textContent = "实验结果尚未就绪";
    $("ablation-synthetic-banner").hidden = true;
    $("ablation-meta").textContent = "";
    $("ablation-filters").textContent = "";
    $("ablation-filter-note").textContent = "";
    $("ablation-cases").textContent = "";
    $("ablation-list").textContent = "";
    const root = $("ablation-detail");
    root.textContent = "";
    const box = el("div", "error-box compare-empty");
    box.appendChild(el("b", null, "实验结果尚未就绪"));
    box.appendChild(document.createTextNode(`：${message}`));
    box.appendChild(el("small", null, "需要：配置的运行目录内含 run-report.json，其哈希绑定的 predictions.jsonl、requests.jsonl、run-config.json、ablation-plan.json 完整一致；预注册方案哈希与登记值一致；ablation-scores.json 与对预测的离线重算一致。来源不符时拒绝展示，不回退演示数据。"));
    root.appendChild(box);
    const demo = el("button", "compact-button", "载入界面测试数据（合成 · 仅验证页面状态）");
    demo.addEventListener("click", () => loadAblation(true));
    root.appendChild(demo);
  }

  async function loadAblation(synthetic = false) {
    if (ablationRequested && !synthetic) return;
    ablationRequested = true;
    try {
      const resp = await fetch(synthetic ? "/api/ablation-comparison?fixture=synthetic" : "/api/ablation-comparison");
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || "实验结果不可用");
      ablationData = data;
      ablationSelection = null;
      renderAblationHero(data);
      renderAblationFilters(data);
      renderAblationList(data);
    } catch (exc) {
      if (synthetic) {
        renderAblationEmpty(`合成界面数据也无法载入：${exc.message}`);
        return;
      }
      renderAblationEmpty(exc.message || "实验结果尚未就绪。");
    }
  }

  // --------------------------------------------------- 60-second guided tour
  // Read-only four-stop tour ("60 秒看懂项目").  Every stop reuses an existing
  // view and its own data loading; the tour only issues the same GET requests
  // those views already make.  It never calls POST /api/run, never calls a
  // model and never executes candidate code.  URL parameter priority:
  // ?tour=1 wins over ?case= (the tour selects its own cases); ?recording=1
  // stays in effect, but the nine-scene presenter bar is hidden while the
  // tour bar is open so two control bars never compete, and restored on exit.

  function tourActive() {
    return tourIndex >= 0;
  }

  const TOUR_STOPS = [
    {
      key: "tuple_str_int",
      title: "tuple_str_int：测试通过，过程承诺仍可能有问题",
      note: "官方测试通过，并不证明解答中的每项承诺都成立。",
      status: "重点案例 · 静态推演摘录（未新增执行）· 预测后 AI 辅助标签复核",
      async go() {
        showView("cases");
        const ok = await loadFeaturedCase("tuple_str_int");
        if (!ok) {
          return {
            target: null,
            message: "重点案例来源暂不可用或校验未通过；页面未用其他内容顶替。仍可前进或退出导览。",
          };
        }
        return {
          target: $("featured-detail"),
          message: "已定位重点案例摘录：高亮 “(5,)” 边界承诺与相关代码；完整限制说明在证据区内。",
        };
      },
    },
    {
      key: "find_char_long",
      title: "find_char_long：把计划—代码失配落到具体位置",
      note: "系统把计划与代码的差异关联到具体步骤和代码位置。",
      status: "构造探针 · 审核金标 S2 与模型实际代码定位 L2 分列 · 未宣称步骤或精确位置命中",
      async go() {
        showView("cases");
        const ok = await loadFeaturedCase("find_char_long");
        if (!ok) {
          return {
            target: null,
            message: "重点案例来源暂不可用或校验未通过；页面未用其他内容顶替。仍可前进或退出导览。",
          };
        }
        return {
          target: $("featured-detail"),
          message: "已定位构造探针：并排查看计划 S2 的 “>= 4” 与代码 L2 的 “> 4”。",
        };
      },
    },
    {
      key: "certificate_replay",
      title: "已有证书重放结果：判断需要可核查证据",
      note: "有执行支持的错误，可以保存反例并核查已有重放结果。",
      status: "历史公开案例 boundary_deletion · 哈希校验来源 · 导览不启动评估、模型调用或候选执行",
      async go() {
        showView("cases");
        if (!showcaseData) {
          return {
            target: $("case-detail"),
            title: "证书重放结果暂不可用",
            note: "公开来源未通过校验，不能确认该站的重放证据。",
            message: "公开证书材料不可用或校验未通过；下方为页面自身的真实不可用提示，未伪造重放结果。仍可前进或退出导览。",
          };
        }
        selectCase("boundary_deletion");
        const replay = regressionData?.metrics?.counterexample_replay_pass;
        const source = regressionData?.source;
        if (!replay || replay.denominator !== 1 || ![0, 1].includes(replay.numerator)
            || !source?.replay_receipt || !source?.replay_receipt_id
            || source.public_counterfactual_sha256 !== showcaseData.source.counterfactual_sha256) {
          return {
            target: $("regression-grid"),
            title: "证书重放结果暂不可用",
            note: "公开案例说明仍可查看，但不能替代通过核验的重放回执。",
            message: "重放回执不可用或来源未通过校验；未伪造重放结果。仍可前进或退出导览。",
          };
        }
        const target = document.querySelector('[data-regression-metric="counterexample_replay_pass"]');
        return {
          target,
          title: replay.numerator === 1 ? "已有证书重放结果：判断需要可核查证据" : "已有重放记录：未确认失败复现",
          note: replay.numerator === 1 ? "公开历史回执记录了失败复现与证据哈希核对；导览未新增执行。" : "已读取历史回执，但失败复现与证据哈希核对未同时通过。",
          message: `已定位回执汇总 ${replay.numerator}/${replay.denominator} · 回执 ${source.replay_receipt_id} · 来源 ${source.replay_receipt} · 与公开案例来源绑定；可下载回归卡片核对。`,
        };
      },
    },
    {
      key: "current_validation",
      title: "最新验证：说明能力与边界",
      note: "检测、精确定位与稳定性分别验证，结果同时披露收益和不足。",
      status: "2026-09-10 最新补充验证 · 开发集 / 五轮消融 / 构造探针口径独立 · 不合并分母、不设总榜",
      async go() {
        showView("demo");
        const overviewSection = $("overview");
        const card = document.querySelector(".current-validation");
        if (overviewSection.hidden) {
          return {
            target: null,
            message: "汇总区域不可用（来源未通过一致性校验）；本页不填零、不猜测、不展示过期成绩。仍可退出导览。",
          };
        }
        if (!currentValidationReady) {
          return {
            target: card,
            reveal: overviewSection,
            message: "最新补充验证暂不可用（来源未通过校验）；下方为真实降级提示与证书说明入口，未展示上一站内容顶替。",
          };
        }
        return {
          target: card,
          reveal: overviewSection,
          message: "已定位最新补充验证：三个实验分卡列示收益与不足，与历史 57 条冻结研究分列。",
        };
      },
    },
  ];

  function currentViewName() {
    for (const view of ["demo", "cases", "compare", "ablation"]) {
      if (!$(`page-${view}`).hidden) return view;
    }
    return "demo";
  }

  function clearTourTarget() {
    document.querySelectorAll(".tour-target, .tour-reveal").forEach((node) => {
      node.classList.remove("tour-target", "tour-reveal");
    });
  }

  async function goToStop(index) {
    if (index < 0 || index >= TOUR_STOPS.length) return;
    const navigation = ++tourNavigation;
    tourIndex = index;
    const stop = TOUR_STOPS[index];
    $("tour-progress").textContent = `${index + 1} / ${TOUR_STOPS.length} · 60 秒看懂项目`;
    $("tour-title").textContent = stop.title;
    $("tour-note").textContent = stop.note;
    $("tour-status").textContent = stop.status;
    $("tour-prev").disabled = index === 0;
    $("tour-next").disabled = index === TOUR_STOPS.length - 1;
    clearTourTarget();
    let outcome;
    try {
      outcome = await stop.go();
    } catch {
      outcome = {
        target: null,
        message: "该站内容加载失败；未展示其他站的过期内容。仍可前进或退出导览。",
      };
    }
    if (navigation !== tourNavigation || index !== tourIndex) return;
    if (outcome.reveal) outcome.reveal.classList.add("tour-reveal");
    if (outcome.target) {
      outcome.target.classList.add("tour-target");
      outcome.target.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
    if (outcome.message) $("tour-status").textContent = `${stop.status} · ${outcome.message}`;
    if (outcome.title) $("tour-title").textContent = outcome.title;
    if (outcome.note) $("tour-note").textContent = outcome.note;
    $("tour-title").focus({ preventScroll: true });
  }

  function startTour() {
    if (!tourActive()) {
      tourReturn = {
        view: currentViewName(), scene: sceneIndex, scrollY: window.scrollY,
        featured: featuredSelection,
        publicCase: document.querySelector(".case-option.active")?.dataset.caseId,
        openDetails: Array.from($("featured-detail").querySelectorAll("details"), node => node.open),
      };
      document.body.classList.add("tour-active");
      $("tour-bar").hidden = false;
      $("presenter").hidden = true; // never show presenter and tour bars together
    }
    goToStop(0);
  }

  async function exitTour() {
    if (!tourActive()) return;
    const back = tourReturn || { view: "demo", scene: 0, scrollY: 0 };
    const navigation = ++tourNavigation;
    ++featuredLoadVersion; // A late tour request must not overwrite the restored case.
    tourIndex = -1;
    tourReturn = null;
    clearTourTarget();
    document.body.classList.remove("tour-active");
    $("tour-bar").hidden = true;
    featuredSelection = back.featured || featuredSelection;
    showView(back.view, undefined, { restore: true });
    if (RECORDING) selectScene(back.scene);
    if (back.view === "cases") {
      selectCase(back.publicCase);
      await loadFeaturedCase(featuredSelection);
      if (navigation !== tourNavigation || currentViewName() !== back.view) return;
      $("featured-detail").querySelectorAll("details").forEach((node, index) => {
        node.open = Boolean(back.openDetails?.[index]);
      });
    }
    if (navigation !== tourNavigation || currentViewName() !== back.view) return;
    $("open-tour").focus({ preventScroll: true });
    // Let reopened details finish layout/scroll anchoring before restoring the viewport.
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (navigation !== tourNavigation || currentViewName() !== back.view) return;
    window.scrollTo({ top: back.scrollY, behavior: "instant" });
  }

  function initTour() {
    $("open-tour").addEventListener("click", startTour);
    $("tour-prev").addEventListener("click", () => goToStop(tourIndex - 1));
    $("tour-next").addEventListener("click", () => goToStop(tourIndex + 1));
    $("tour-exit").addEventListener("click", exitTour);
    document.addEventListener("keydown", (event) => {
      if (!tourActive()) return;
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        exitTour();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        goToStop(tourIndex + 1);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        goToStop(tourIndex - 1);
      }
    });
  }

  // ----------------------------------------------------------------- init

  async function init() {
    initPresenter();
    initTour();
    $("open-featured").addEventListener("click", () => showView("cases"));
    document.querySelectorAll("[data-featured-case]").forEach(button => {
      button.addEventListener("click", () => loadFeaturedCase(button.dataset.featuredCase));
    });
    $("open-costs").addEventListener("click", () => {
      showView("demo");
      if (RECORDING) selectScene(8);
      else $("card-costs").scrollIntoView({behavior: "smooth", block: "start"});
    });
    document.querySelectorAll(".view-button").forEach((button) => {
      button.addEventListener("click", () => showView(button.dataset.view));
    });
    document.querySelectorAll(".matrix-cell[data-case]").forEach((button) => {
      button.addEventListener("click", () => showView("cases", button.dataset.case));
    });
    $("download-regression").addEventListener("click", () => {
      if (!regressionData) return;
      const link = document.createElement("a");
      link.href = "/api/regression";
      link.download = `tracejudge_regression_${regressionData.evaluator_version}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
    });
    $("btn-start").addEventListener("click", startRun);
    $("btn-reset").addEventListener("click", () => {
      if (!running) resetRunUI();
    });
    try {
      const resp = await fetch("/api/status");
      const status = await resp.json();
      document.title = `TraceJudge-Hy3 ${status.app.version} · 过程评估工作台`;
      renderProblem(status.problem);
      setStage(1, "done");
      setCaption(1, "done");
      const hy3 = status.modes.hy3;
      const note = $("hy3-availability");
      if (hy3.available) {
        note.textContent = "已配置且 Docker 可用";
      } else {
        note.textContent = hy3.configured
          ? "已配置，但 Docker 不可用"
          : "未配置 HY3 环境变量，当前不可用";
        $("option-hy3").classList.add("disabled");
        $("option-hy3").querySelector("input").disabled = true;
      }
      foot("就绪");
    } catch {
      $("problem-body").textContent = "无法连接本地演示服务，请确认服务已启动。";
      foot("服务未连接");
    }
    await Promise.all([loadOverview(false), loadShowcase(), loadRegression(), loadMethodCosts(), loadCurrentValidation()]);
    // URL parameter priority: ?tour=1 overrides ?case= (the tour selects its
    // own cases); ?recording=1 combines freely with either.
    if (params.get("tour") === "1") startTour();
    else if (["tuple_str_int", "find_char_long"].includes(params.get("case"))) showView("cases");
  }

  init();
})();
