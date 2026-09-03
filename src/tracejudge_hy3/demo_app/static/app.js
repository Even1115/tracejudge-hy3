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
  if (params.get("recording") === "1") document.body.classList.add("recording");

  const REVEAL_DELAY_MS = 1300;
  const POLL_MS = 600;

  let currentMode = "fixture";
  let running = false;
  let currentRunId = null;
  let showcaseData = null;
  let regressionData = null;

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
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
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

  function showView(view, caseId) {
    const cases = view === "cases";
    $("page-demo").hidden = cases;
    $("page-cases").hidden = !cases;
    document.querySelectorAll(".view-button").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    if (cases && showcaseData) {
      selectCase(caseId || showcaseData.cases[0].case_id);
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
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

    const pre = el("pre", "code");
    s.code.split("\n").forEach((line, i) => {
      pre.appendChild(el("span", "lineno", String(i + 1)));
      pre.appendChild(document.createTextNode(line + "\n"));
    });
    body.appendChild(pre);
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
    kv(dl, "functional_correct", fmtBool(a.functional_correct, "true", "false"), true);
    kv(dl, "process_correct", fmtBool(a.process_correct, "true", "false"), true);
    kv(dl, "first_faulty_layer", a.first_faulty_layer, true);
    kv(dl, "first_faulty_step", a.first_faulty_step, true);
    kv(dl, "violated_requirement", a.violated_requirement, true);
    kv(dl, "error_type", a.error_type, true);
    kv(dl, "代码位置 code_span", a.code_span, true);
    if (a.secondary_error_types && a.secondary_error_types.length) {
      kv(dl, "次要错误类型", a.secondary_error_types.join(", "), true);
    }
    body.appendChild(dl);
    if (a.explanation) body.appendChild(el("p", "explanation", a.explanation));
  }

  function renderCertificate(data) {
    const body = $("certificate-body");
    body.textContent = "";
    const ce = data.counterexample;
    const cert = data.certificate;

    if (ce) {
      const box = el("div", "counterexample-box");
      box.appendChild(el("span", null, "最小反例 MINIMAL COUNTEREXAMPLE"));
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
      kv(dl, "错误证书", "未产生（首次运行未发现错误）");
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
    const body = $("solution-body");
    body.textContent = "";
    const box = el("div", "error-box");
    box.appendChild(el("b", null, "运行未完成"));
    box.appendChild(document.createTextNode(`：${data.error}`));
    box.appendChild(el("small", null, `error_type = ${data.error_type} · 页面未切换到 Mock，未展示预设结果`));
    body.appendChild(box);
    revealCard("card-solution");
  }

  function renderOverview(o, shouldScroll) {
    const section = $("overview");
    section.hidden = false;
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
      document.createTextNode(` 第二标注者独立复标，agreement = ${review.agreement_status}（尚未计算）`)
    );
    extra.appendChild(r2);
    for (const row of o.difficulty) {
      const d = el("div");
      d.appendChild(el("b", null, `${row.passed}/${row.included}`));
      d.appendChild(document.createTextNode(` ${row.tier} Base+Plus 通过（代理难度分层）`));
      extra.appendChild(d);
    }
    $("overview-disclaimer").textContent = o.disclaimer;
    if (shouldScroll) section.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
    try {
      const resp = await fetch("/api/regression");
      if (!resp.ok) throw new Error("regression unavailable");
      renderRegression(await resp.json());
    } catch {
      $("regression-grid").textContent = "回归卡片生成失败；未展示占位指标。";
    }
  }

  // ------------------------------------------------------------- run flow

  function resetRunUI() {
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
  }

  function setRunningUI(mode) {
    $("mode-badge").className = `mode-badge ${mode}`;
    $("mode-badge").textContent =
      mode === "fixture" ? "公开 FIXTURE · 未调用真实 HY3" : "真实 HY3 · DOCKER 沙盒";
    $("caption-generate").textContent =
      mode === "fixture" ? "Mock Solver 生成结构化解答" : "Hy3 生成结构化解答";
    for (const n of [2, 3, 4, 5, 6]) setStage(n, "");
    setStage(2, "running");
    setCaption(2, "active");
    foot("流水线真实执行中…");
    $("run-meta").textContent = "";
  }

  async function revealResult(data) {
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
      await new Promise((resolve) => setTimeout(resolve, REVEAL_DELAY_MS));
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
      await pollRun(runId);
    } catch (exc) {
      running = false;
      $("btn-start").disabled = false;
      renderError({ error: `无法连接本地演示服务：${exc.message}`, error_type: "NetworkError" });
      setStage(2, "failed");
      foot("请求失败");
    }
  }

  // ----------------------------------------------------------------- init

  async function init() {
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
        $("option-hy3 input").disabled = true;
      }
      foot("就绪");
    } catch {
      $("problem-body").textContent = "无法连接本地演示服务，请确认服务已启动。";
      foot("服务未连接");
    }
    await Promise.all([loadOverview(false), loadShowcase(), loadRegression()]);
  }

  init();
})();
