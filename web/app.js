// 工单主题观察台 —— 前端交互
// 所有来自后端的文本（主题名、工单标题等）都用 textContent 写入，避免注入。

const state = {
  overview: null,
  topics: [],
  config: null,
  history: null,
  pendingFilter: "",
  currentTab: "topics",
  lastRunMapping: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

let toastTimer;
function toast(message, kind = "") {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.className = "toast"), 2800);
}

function fmtPct(x) {
  return x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`;
}

function monthLabel(m) {
  if (!m || m === "未知") return "未知";
  const [y, mo] = m.split("-");
  return `${Number(mo)} 月`;
}

const colorVar = (slot) => `var(--series-${((slot - 1) % 8) + 1})`;

// ---------------------------------------------------------------------------
// 图表基元
// ---------------------------------------------------------------------------

function niceMax(value) {
  if (value <= 4) return 4;
  const pow = Math.pow(10, Math.floor(Math.log10(value)));
  for (const mult of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (mult * pow >= value) return mult * pow;
  }
  return 10 * pow;
}

function roundedTop(x, y, w, h, r) {
  if (h <= 0) return "";
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y}` +
    ` L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + h} Z`;
}

/**
 * 堆叠柱状图：系列按顺序自底向上堆叠，柱顶 4px 圆角，柱间 2px 表面色间隙。
 * labels/values 都是数字数组；tooltip 逐列显示全部系列。
 */
function stackedColumns(host, months, series, { height = 250, ariaLabel = "" } = {}) {
  host.textContent = "";
  if (!months.length) {
    host.append(el("p", { class: "empty", text: "暂无数据" }));
    return;
  }
  const width = 760;
  const margin = { top: 18, right: 16, bottom: 30, left: 38 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const totals = months.map((m) => series.reduce((sum, s) => sum + (m[s.key] || 0), 0));
  const max = niceMax(Math.max(...totals, 1));
  const band = plotW / months.length;
  const barW = Math.min(24, band * 0.52);
  const y = (v) => margin.top + plotH - (v / max) * plotH;
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", ariaLabel || "按月工单量");

  const add = (tag, attrs, text) => {
    const node = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      if ((k === "fill" || k === "stroke") && String(v).startsWith("var(")) {
        node.style[k] = v;
      } else {
        node.setAttribute(k, v);
      }
    }
    if (text !== undefined) node.textContent = text;
    svg.append(node);
    return node;
  };

  // 网格与刻度
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const value = (max / ticks) * i;
    const yy = y(value);
    add("line", { x1: margin.left, x2: width - margin.right, y1: yy, y2: yy, stroke: "var(--grid)", "stroke-width": 1 });
    add("text", {
      x: margin.left - 8, y: yy + 4, "text-anchor": "end",
      "font-size": 11, fill: "var(--text-muted)",
    }, String(Math.round(value)));
  }

  months.forEach((m, i) => {
    const cx = margin.left + band * i + band / 2;
    let acc = 0;
    const stack = series.filter((s) => (m[s.key] || 0) > 0);
    stack.forEach((s, si) => {
      const value = m[s.key] || 0;
      const h = (value / max) * plotH;
      const gap = si > 0 ? 2 : 0;
      const yTop = y(acc + value) + (si > 0 ? 0 : 0);
      const rectY = yTop;
      const rectH = Math.max(0, h - gap);
      const path = document.createElementNS(svgNS, "path");
      const isTop = si === stack.length - 1;
      const d = isTop
        ? roundedTop(cx - barW / 2, rectY, barW, rectH, 4)
        : `M${cx - barW / 2},${rectY} h${barW} v${rectH} h${-barW} Z`;
      path.setAttribute("d", d);
      path.style.fill = colorVar(s.slot);
      path.dataset.series = s.key;
      svg.append(path);
      acc += value;
    });
    add("text", {
      x: cx, y: height - 8, "text-anchor": "middle", "font-size": 11, fill: "var(--text-muted)",
    }, monthLabel(m.month));
    // 只标注最高的一个月，其余交给坐标轴与悬浮层
    if (totals[i] === Math.max(...totals) && totals[i] > 0) {
      add("text", {
        x: cx, y: y(totals[i]) - 7, "text-anchor": "middle", "font-size": 11.5,
        fill: "var(--text-primary)", "font-weight": 600,
      }, String(totals[i]));
    }
  });
  add("line", {
    x1: margin.left, x2: width - margin.right, y1: y(0), y2: y(0),
    stroke: "var(--axis)", "stroke-width": 1,
  });

  // 悬浮层：整列都可命中
  const hit = document.createElementNS(svgNS, "g");
  months.forEach((m, i) => {
    const rect = document.createElementNS(svgNS, "rect");
    rect.setAttribute("x", margin.left + band * i);
    rect.setAttribute("y", margin.top);
    rect.setAttribute("width", band);
    rect.setAttribute("height", plotH);
    rect.setAttribute("fill", "transparent");
    rect.setAttribute("tabindex", "0");
    rect.setAttribute("role", "button");
    rect.setAttribute("aria-label", `${monthLabel(m.month)}：${series.map((s) => `${s.label} ${m[s.key] || 0}`).join("，")}`);
    const show = (evt) => showTip(host, evt, m, series, band * i + margin.left + band / 2, width);
    rect.addEventListener("pointermove", show);
    rect.addEventListener("focus", (evt) => showTip(host, evt, m, series, band * i + margin.left + band / 2, width, true));
    rect.addEventListener("pointerleave", () => hideTip(host));
    rect.addEventListener("blur", () => hideTip(host));
    hit.append(rect);
  });
  svg.append(hit);
  host.append(svg);

  // 图例（两个以上系列）
  if (series.length >= 2) {
    const legend = el("div", { class: "legend-inline" });
    for (const s of series) {
      legend.append(el("span", {}, [
        el("i", { style: `background:${colorVar(s.slot)}` }),
        s.label,
      ]));
    }
    host.append(legend);
  }

  const tip = el("div", { class: "chart-tip" });
  host.append(tip);
}

function showTip(host, evt, month, series, xData, viewWidth, keyboard = false) {
  const tip = host.querySelector(".chart-tip");
  if (!tip) return;
  tip.textContent = "";
  tip.append(el("div", { class: "tip-title", text: monthLabel(month.month) }));
  let total = 0;
  for (const s of series) {
    const value = month[s.key] || 0;
    total += value;
    tip.append(el("div", { class: "row" }, [
      el("span", { class: "k" }, [el("span", { class: "key", style: `color:${colorVar(s.slot)}` }), s.label]),
      el("span", { class: "v", text: String(value) }),
    ]));
  }
  if (series.length > 1) {
    tip.append(el("div", { class: "row" }, [
      el("span", { class: "k", text: "合计" }),
      el("span", { class: "v", text: String(total) }),
    ]));
  }
  const svg = host.querySelector("svg");
  const rect = svg.getBoundingClientRect();
  const hostRect = host.getBoundingClientRect();
  const scale = rect.width / viewWidth;
  const px = rect.left - hostRect.left + xData * scale;
  tip.style.left = `${Math.max(70, Math.min(hostRect.width - 70, px))}px`;
  tip.style.top = `${rect.top - hostRect.top + 8}px`;
  tip.classList.add("show");
}

function hideTip(host) {
  const tip = host.querySelector(".chart-tip");
  if (tip) tip.classList.remove("show");
}

function sparkline(months, { width = 260, height = 34, color = "var(--text-secondary)" } = {}) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "mini-trend");
  svg.setAttribute("aria-hidden", "true");
  if (!months.length) return svg;
  const values = months.map((m) => m.tickets);
  const max = Math.max(...values, 1);
  const stepX = months.length > 1 ? (width - 10) / (months.length - 1) : 0;
  const pts = values.map((v, i) => [5 + i * stepX, height - 5 - (v / max) * (height - 12)]);
  if (pts.length === 1) {
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", pts[0][0]);
    dot.setAttribute("cy", pts[0][1]);
    dot.setAttribute("r", 3.5);
    dot.style.fill = color;
    svg.append(dot);
    return svg;
  }
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
  const area = `${line} L${pts.at(-1)[0]},${height - 2} L${pts[0][0]},${height - 2} Z`;
  const areaNode = document.createElementNS(ns, "path");
  areaNode.setAttribute("d", area);
  areaNode.style.fill = color;
  areaNode.setAttribute("opacity", "0.12");
  svg.append(areaNode);
  const lineNode = document.createElementNS(ns, "path");
  lineNode.setAttribute("d", line);
  lineNode.style.fill = "none";
  lineNode.style.stroke = color;
  lineNode.setAttribute("stroke-width", "2");
  lineNode.setAttribute("stroke-linecap", "round");
  lineNode.setAttribute("stroke-linejoin", "round");
  svg.append(lineNode);
  const end = pts.at(-1);
  const dot = document.createElementNS(ns, "circle");
  dot.setAttribute("cx", end[0]);
  dot.setAttribute("cy", end[1]);
  dot.setAttribute("r", 4);
  dot.style.fill = color;
  dot.style.stroke = "var(--surface-1)";
  dot.setAttribute("stroke-width", "2");
  svg.append(dot);
  return svg;
}

// ---------------------------------------------------------------------------
// 数据刷新
// ---------------------------------------------------------------------------

async function refreshAll() {
  const [overview, topics, config, history, pending, documents] = await Promise.all([
    api("/api/overview"),
    api("/api/topics"),
    api("/api/config"),
    api("/api/history"),
    api("/api/pending"),
    api("/api/documents"),
  ]);
  state.overview = overview;
  state.topics = topics.topics;
  state.config = config;
  state.history = history;
  state.pending = pending;
  state.docCache = Object.fromEntries(documents.documents.map((d) => [d.id, d]));
  state.samples = documents.documents.filter((d) => d.segments.length > 3 && d.original_chars >= 40);
  state.lastRunMapping = history.runs.find((r) => r.kind === "train" || r.mapping?.length) || history.runs[0];
  renderStats();
  renderOverallChart();
  renderTopics();
  renderPending();
  renderBatches();
  renderRuns();
  renderRules();
  renderHistory();
  $("#pending-count").textContent = String(overview.pending);
}

// ---------------------------------------------------------------------------
// 概览
// ---------------------------------------------------------------------------

function renderStats() {
  const o = state.overview;
  const months = o.overall_trend.months;
  const last = months.at(-1);
  const prev = months.at(-2);
  const delta = last && prev ? last.tickets - prev.tickets : null;
  const row = $("#stat-row");
  row.textContent = "";
  const tiles = [
    { label: "工单总数", value: o.documents, note: "含重复投递" },
    {
      label: "去重后工单",
      value: o.documents - o.duplicates,
      note: delta === null ? "按首次投递计数" : `${monthLabel(last.month)} 较上月 ${delta > 0 ? "+" : ""}${delta}`,
      deltaClass: delta > 0 ? "up" : delta < 0 ? "down" : "",
    },
    { label: "重复投递", value: o.duplicates, note: "已并入首次投递，不抬升趋势" },
    { label: "待整理", value: o.pending, note: `短文本 ${o.short} · 空文本 ${o.empty} · 离群 ${o.outliers}` },
    { label: "主题", value: o.topics, note: `已确认 ${o.confirmed}` },
  ];
  for (const t of tiles) {
    row.append(el("div", { class: "stat" }, [
      el("div", { class: "label", text: t.label }),
      el("div", { class: "value", text: String(t.value) }),
      el("div", { class: `delta ${t.deltaClass || ""}`, text: t.note }),
    ]));
  }
}

function renderOverallChart() {
  const months = state.overview.overall_trend.months;
  stackedColumns($("#overall-chart"), months, [
    { key: "tickets", label: "去重后工单", slot: 1 },
    { key: "duplicates", label: "重复投递（不计入）", slot: 2 },
    { key: "pending", label: "待整理", slot: 3 },
  ], { ariaLabel: "每月工单量（去重后、重复投递、待整理）" });
  const table = $("#overall-table");
  table.textContent = "";
  const t = el("table", { class: "data" });
  t.append(el("thead", {}, el("tr", {}, [
    el("th", { text: "月份" }), el("th", { text: "去重后工单" }),
    el("th", { text: "重复投递" }), el("th", { text: "待整理" }),
  ])));
  const body = el("tbody");
  for (const m of months) {
    body.append(el("tr", {}, [
      el("td", { text: m.month }), el("td", { class: "num", text: String(m.tickets) }),
      el("td", { class: "num", text: String(m.duplicates) }), el("td", { class: "num", text: String(m.pending) }),
    ]));
  }
  t.append(body);
  table.append(t);
  const dup = state.overview.duplicates;
  $("#dup-note").textContent = dup
    ? `共 ${dup} 条重复投递已并入首次投递：趋势按“去重后的工单”画，转发再多也不会被算成新增投诉。`
    : "当前没有发现重复投递。";
}

$$(".seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
    const chart = btn.dataset.view === "chart";
    $("#overall-chart").hidden = !chart;
    $("#overall-table").hidden = chart;
  });
});

// ---------------------------------------------------------------------------
// 主题卡片
// ---------------------------------------------------------------------------

function renderTopics() {
  const grid = $("#topic-grid");
  grid.textContent = "";
  const topics = state.topics.filter((t) => ["confirmed", "proposed"].includes(t.status));
  const others = state.topics.filter((t) => !["confirmed", "proposed"].includes(t.status));
  if (!topics.length) {
    grid.append(el("p", { class: "empty", text: "还没有主题：先导入工单并训练。" }));
  }
  for (const topic of topics) {
    const card = el("button", { class: "topic-card", type: "button" });
    const head = el("div", { class: "topic-head" }, [
      el("span", { class: "topic-swatch", style: `background:${colorVar(topic.color_slot)}` }),
      el("span", { class: "topic-name", text: topic.name }),
      el("span", { class: "topic-count", text: `${topic.canonical_count} 单` }),
    ]);
    card.append(head);
    const chips = el("div", { class: "chip-row" });
    chips.append(el("span", { class: `badge ${topic.status}`, text: topic.status === "confirmed" ? "已确认" : "候选" }));
    if (topic.name_is_custom) chips.append(el("span", { class: "badge", text: "人工命名" }));
    if (topic.keywords?.length) {
      for (const kw of topic.keywords.slice(0, 4)) {
        chips.append(el("span", { class: "chip" }, [
          el("span", { class: "dot", style: `background:${colorVar(topic.color_slot)}` }),
          kw.term,
        ]));
      }
    }
    card.append(chips);
    if (topic.trend.length) card.append(sparkline(topic.trend, { color: colorVar(topic.color_slot) }));
    const rep = topic.representatives[0];
    card.append(el("div", { class: "rep-line" }, [
      el("b", { text: rep ? `代表：${rep.subject}` : "暂无代表工单" }),
      rep ? ` · ${rep.date}` : "",
    ]));
    card.addEventListener("click", () => openTopic(topic.id));
    grid.append(card);
  }
  if (others.length) {
    const note = el("p", { class: "muted", style: "grid-column:1/-1;margin-top:4px" },
      `另有 ${others.length} 个已归档/已合并主题，可在「历史与回看」中查看。`);
    grid.append(note);
  }
}

// ---------------------------------------------------------------------------
// 主题详情
// ---------------------------------------------------------------------------

async function openTopic(topicId) {
  const topic = await api(`/api/topic?id=${topicId}`);
  const body = $("#dialog-body");
  body.textContent = "";
  const head = el("div", { class: "detail-head" }, [
    el("span", { class: "topic-swatch", style: `background:${colorVar(topic.color_slot)};width:12px;height:12px` }),
    el("h2", { text: topic.name }),
    el("span", { class: `badge ${topic.status}`, text: topic.status === "confirmed" ? "已确认" : topic.status === "merged" ? "已合并" : "候选" }),
  ]);
  body.append(head);
  body.append(el("p", { class: "detail-sub", text: `${topic.documents.length} 条关联工单（含重复投递）· 平均相似度 ${fmtPct(topic.cohesion)}` }));
  if (topic.merged_children?.length) {
    body.append(el("p", { class: "detail-sub", text: `已并入：${topic.merged_children.map((c) => c.name).join("、")}` }));
  }

  const actions = el("div", { class: "detail-actions" });
  const renameBtn = el("button", { class: "btn small", text: "改名" });
  renameBtn.addEventListener("click", async () => {
    const name = prompt("新的主题名称", topic.name);
    if (!name || name.trim() === topic.name) return;
    try {
      await api("/api/topic/rename", { method: "POST", body: JSON.stringify({ id: topic.id, name }) });
      toast("主题已改名");
      closeDialog();
      await refreshAll();
    } catch (err) { toast(err.message, "error"); }
  });
  actions.append(renameBtn);

  if (topic.status !== "confirmed") {
    const confirmBtn = el("button", { class: "btn small", text: "确认为主题" });
    confirmBtn.addEventListener("click", async () => {
      try {
        await api("/api/topic/confirm", { method: "POST", body: JSON.stringify({ id: topic.id }) });
        toast("已确认，后续批次将优先匹配");
        closeDialog();
        await refreshAll();
      } catch (err) { toast(err.message, "error"); }
    });
    actions.append(confirmBtn);
  }

  const targets = state.topics.filter((t) => t.id !== topic.id && ["confirmed", "proposed"].includes(t.status));
  if (targets.length) {
    const select = el("select", { "aria-label": "合并到其他主题" });
    select.append(el("option", { value: "", text: "合并到…" }));
    for (const t of targets) select.append(el("option", { value: String(t.id), text: t.name }));
    select.addEventListener("change", async () => {
      if (!select.value) return;
      const target = targets.find((t) => String(t.id) === select.value);
      if (!confirm(`把「${topic.name}」合并进「${target.name}」？历史趋势会按目标主题回看。`)) {
        select.value = "";
        return;
      }
      try {
        await api("/api/topic/merge", { method: "POST", body: JSON.stringify({ source: topic.id, target: Number(select.value) }) });
        toast("已合并");
        closeDialog();
        await refreshAll();
      } catch (err) { toast(err.message, "error"); }
    });
    actions.append(select);
  }
  body.append(actions);

  body.append(el("div", { class: "detail-section" }, [
    el("h3", { text: "为什么这些工单被放在一起" }),
    el("ul", { class: "why-list" }, topic.why.map((w) => el("li", { text: w }))),
  ]));

  if (topic.keywords?.length) {
    const chips = el("div", { class: "chip-row", style: "margin-top:8px" });
    for (const kw of topic.keywords) {
      chips.append(el("span", { class: "chip", text: `${kw.term} · ${kw.docs} 条` }));
    }
    body.append(el("div", { class: "detail-section" }, [
      el("h3", { text: "归类线索（仅供参考）" }),
      chips,
      el("p", { class: "muted", style: "margin-top:4px", text: "关键词由聚类中心的词特征还原，不代表原文作出了该结论。" }),
    ]));
  }

  body.append(el("div", { class: "detail-section" }, [
    el("h3", { text: "主题随时间的变化" }),
    el("div", { id: "detail-trend", class: "detail-trend" }),
  ]));
  stackedColumns($("#detail-trend", body), topic.trend, [
    { key: "tickets", label: "去重后工单", slot: topic.color_slot },
    { key: "mentions", label: "含重复提及", slot: 4 },
  ], { height: 210, ariaLabel: "该主题每月工单量" });

  body.append(el("div", { class: "detail-section" }, [
    el("h3", { text: "代表工单" }),
    el("div", {}, topic.representatives.map((r) => el("div", { class: "doc-card" }, [
      el("div", { class: "meta" }, [
        el("span", { text: `#${r.id}` }), el("span", { text: r.date }), el("span", { text: r.method_label }),
        el("span", { text: `相似度 ${fmtPct(r.similarity)}` }),
      ]),
      el("div", { class: "title", text: r.subject || "（无标题）" }),
      el("div", { class: "body", text: r.excerpt }),
    ]))),
  ]));

  body.append(el("div", { class: "detail-section" }, [
    el("h3", { text: `全部工单（${topic.documents.length}）` }),
    el("div", { class: "doc-list" }, topic.documents.map((doc) => docRow(doc, topic.id))),
  ]));

  openDialog();
}

function docRow(doc, currentTopicId) {
  const row = el("div", { class: "doc-row" });
  const info = el("div");
  const meta = el("div", { class: "meta" }, [
    el("span", { text: `#${doc.id}` }),
    el("span", { text: doc.submitted_at }),
    el("span", { text: doc.batch_id }),
    el("span", { text: doc.method_label }),
  ]);
  if (doc.method === "duplicate") meta.append(el("span", { text: `重复于 #${doc.duplicate_of}` }));
  if (doc.similarity !== null && doc.similarity !== undefined && doc.method !== "duplicate") {
    meta.append(el("span", { text: `相似度 ${fmtPct(doc.similarity)}` }));
  }
  info.append(meta);
  info.append(el("div", { class: "title", text: doc.subject || "（无标题）" }));
  info.append(el("div", { class: "excerpt", text: (doc.clean_text || "（清理后为空）").slice(0, 120) }));
  if (doc.reason) info.append(el("div", { class: "why-line", text: doc.reason }));
  row.append(info);

  const actions = el("div", { class: "doc-actions" });
  const topics = state.topics.filter((t) => ["confirmed", "proposed"].includes(t.status));
  const select = el("select", { "aria-label": "移动工单到主题" });
  select.append(el("option", { value: "", text: "移到…" }));
  if (doc.topic_id !== null) select.append(el("option", { value: "none", text: "移入待整理区" }));
  for (const t of topics) {
    if (t.id === doc.topic_id) continue;
    select.append(el("option", { value: String(t.id), text: t.name }));
  }
  select.addEventListener("change", async () => {
    const value = select.value;
    if (!value) return;
    try {
      await api("/api/document/move", {
        method: "POST",
        body: JSON.stringify({ id: doc.id, topic_id: value === "none" ? null : Number(value) }),
      });
      toast("已移动，修改前归类已存入历史");
      closeDialog();
      await refreshAll();
    } catch (err) { toast(err.message, "error"); }
  });
  actions.append(select);
  const detail = el("button", { class: "btn small ghost", text: "原文对照" });
  detail.addEventListener("click", () => openDocument(doc.id));
  actions.append(detail);
  row.append(actions);
  return row;
}

function openDocument(docId) {
  const all = state.docCache?.[docId];
  if (!all) {
    api(`/api/documents`).then(({ documents }) => {
      state.docCache = Object.fromEntries(documents.map((d) => [d.id, d]));
      openDocument(docId);
    });
    return;
  }
  const body = $("#dialog-body");
  body.textContent = "";
  body.append(el("div", { class: "detail-head" }, [
    el("h2", { text: all.subject || "（无标题）" }),
    el("span", { class: "badge", text: all.method_label }),
  ]));
  body.append(el("p", { class: "detail-sub", text: `#${all.id} · ${all.submitted_at} · 批次 ${all.batch_id}` }));
  const cols = el("div", { class: "sample-compare" });
  const left = el("div", { class: "compare-col" }, [
    el("h3", { text: "原文（逐段标注处理方式）" }),
    el("div", { class: "seg-list" }, all.segments.map((seg) => {
      const tag = segKindLabel(seg.kind);
      const text = seg.text && seg.text.trim() ? seg.text : "（已删除的模板噪声）";
      const node = el("div", { class: `seg ${seg.kind}` }, [text]);
      if (tag) node.append(el("span", { class: "tag", text: tag }));
      return node;
    })),
  ]);
  const right = el("div", { class: "compare-col" }, [
    el("h3", { text: `清理后（${all.clean_text.length} 字）` }),
    el("div", { class: "cleaned-box", text: all.clean_text || "（空）" }),
  ]);
  cols.append(left, right);
  body.append(cols);
  body.append(el("p", { class: "muted", style: "margin-top:10px", text:
    `原文 ${all.original_chars} 字 → 清理后 ${all.clean_text.length} 字，共移除 ${all.removed_chars} 字。` }));
  openDialog();
}

const SEG_LABELS = {
  kept: "保留", quote_header: "引用引导行", quoted_line: "引用内容", quoted_block: "被转发的原信",
  reply_chain: "回复链", forward_header: "转发标记", signature: "签名/落款", greeting: "称呼",
  auto_footer: "自动页脚", case_noise: "模板噪声", blank: "空行",
};
const segKindLabel = (kind) => SEG_LABELS[kind] || "";

// ---------------------------------------------------------------------------
// 待整理
// ---------------------------------------------------------------------------

function renderPending() {
  const host = $("#pending-groups");
  host.textContent = "";
  const filter = $("#pending-filter");
  const groups = state.pending?.groups || [];
  const current = filter.value;
  filter.textContent = "";
  filter.append(el("option", { value: "", text: "全部" }));
  for (const g of groups) filter.append(el("option", { value: g.kind, text: `${g.label}（${g.documents.length}）` }));
  filter.value = groups.some((g) => g.kind === current) ? current : "";

  const visible = groups.filter((g) => !filter.value || g.kind === filter.value);
  $("#pending-summary").textContent = `共 ${state.pending?.total || 0} 条；这些记录没有被强行塞进主题。`;
  if (!visible.length) {
    host.append(el("p", { class: "empty", text: "待整理区为空。" }));
    return;
  }
  for (const group of visible) {
    const section = el("section", { class: "pending-group" });
    section.append(el("div", { class: "pending-group-head" }, [
      el("h3", { text: group.label }),
      el("span", { class: "muted", text: `${group.documents.length} 条` }),
    ]));
    const list = el("div", { class: "doc-list" });
    for (const doc of group.documents) {
      const row = el("div", { class: "doc-row" });
      const info = el("div");
      info.append(el("div", { class: "meta" }, [
        el("span", { text: `#${doc.id}` }), el("span", { text: doc.submitted_at }),
        el("span", { text: doc.batch_id }),
        doc.candidate_topic_name ? el("span", { text: `最接近：${doc.candidate_topic_name}` }) : null,
        doc.candidate_similarity ? el("span", { text: `相似度 ${fmtPct(doc.candidate_similarity)}` }) : null,
      ]));
      info.append(el("div", { class: "title", text: doc.subject || "（无标题）" }));
      info.append(el("div", { class: "excerpt", text: (doc.clean_text || "（清理后为空）").slice(0, 120) }));
      if (doc.reason) info.append(el("div", { class: "why-line", text: doc.reason }));
      row.append(info);
      const actions = el("div", { class: "doc-actions" });
      const topics = state.topics.filter((t) => ["confirmed", "proposed"].includes(t.status));
      if (topics.length) {
        const select = el("select", { "aria-label": "归入主题" });
        select.append(el("option", { value: "", text: "归入…" }));
        for (const t of topics) select.append(el("option", { value: String(t.id), text: t.name }));
        select.addEventListener("change", async () => {
          if (!select.value) return;
          try {
            await api("/api/document/move", { method: "POST", body: JSON.stringify({ id: doc.id, topic_id: Number(select.value) }) });
            toast("已归入主题，修改前归类已存入历史");
            await refreshAll();
          } catch (err) { toast(err.message, "error"); }
        });
        actions.append(select);
      }
      const detail = el("button", { class: "btn small ghost", text: "原文对照" });
      detail.addEventListener("click", () => openDocument(doc.id));
      actions.append(detail);
      row.append(actions);
      list.append(row);
    }
    section.append(list);
    host.append(section);
  }
}

$("#pending-filter")?.addEventListener("change", renderPending);

// ---------------------------------------------------------------------------
// 批次与训练
// ---------------------------------------------------------------------------

async function renderBatches() {
  const host = $("#batch-list");
  host.textContent = "";
  const { batches } = await api("/api/batches");
  for (const batch of batches) {
    const imported = batch.imported > 0;
    const row = el("div", { class: "batch-row" });
    row.append(el("div", {}, [
      el("div", { class: "batch-name", text: batch.label }),
      el("div", { class: "meta", text: imported ? `已导入 ${batch.imported} 条` : `${batch.count} 条待导入（虚构演示数据）` }),
    ]));
    const actions = el("div", { class: "doc-actions" });
    if (!imported) {
      const btn = el("button", { class: "btn small", text: "导入并匹配" });
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          const res = await api("/api/import-batch", { method: "POST", body: JSON.stringify({ batch: batch.id, mode: "match" }) });
          toast(`已导入 ${res.imported} 条，匹配 ${res.run.summary.matched} 条，待整理 ${res.run.summary.pending} 条`);
          await refreshAll();
          await renderMapping(res.run.run_id);
        } catch (err) { toast(err.message, "error"); btn.disabled = false; }
      });
      actions.append(btn);
    } else {
      const again = el("button", { class: "btn small ghost", text: "重新匹配" });
      again.addEventListener("click", async () => {
        try {
          const res = await api("/api/match", { method: "POST", body: JSON.stringify({ batch: batch.id }) });
          toast(`匹配 ${res.summary.matched} 条，待整理 ${res.summary.pending} 条`);
          await refreshAll();
          await renderMapping(res.run_id);
        } catch (err) { toast(err.message, "error"); }
      });
      actions.append(again);
    }
    row.append(actions);
    host.append(row);
  }
  await renderMapping();
}

async function renderMapping(runId = null) {
  const host = $("#mapping-list");
  host.textContent = "";
  const runs = state.history?.runs || [];
  const run = runId ? runs.find((r) => r.id === runId) || (await refreshRunsQuiet(runId)) : runs.find((r) => r.kind === "train" || r.mapping?.length);
  if (!run) {
    host.append(el("p", { class: "empty", text: "还没有训练记录。" }));
    return;
  }
  host.append(el("p", { class: "muted", text: `${new Date(run.created_at).toLocaleString("zh-CN")} · ${run.summary.run_label || ""}` }));
  const mapping = run.mapping || [];
  if (!mapping.length) {
    host.append(el("p", { class: "empty", text: "本次是批次匹配，没有重训主题；对应关系会在重新训练后显示。" }));
    return;
  }
  for (const m of mapping) {
    if (m.unmatched) {
      const absorbed = m.absorbed_into ? state.topics.find((t) => t.id === m.absorbed_into) : null;
      host.append(el("div", { class: "mapping-row unmatched" }, [
        el("span", { text: `旧主题「${m.topic_name}」` }),
        el("span", { class: "arrow", text: "→" }),
        absorbed
          ? el("span", {
            text: `未单独沿用；${m.absorbed_count}/${m.member_total} 条成员按内容并入「${absorbed.name}」，其余进入待整理（不凭编号强行续接）`,
          })
          : el("span", { text: "没有内容相近的候选，已归档；成员按相似度重新归类（不凭编号强行续接）" }),
        el("span", { class: "sim muted", text: `最接近 ${fmtPct(m.similarity)}` }),
      ]));
      continue;
    }
    const name = m.topic_name || `主题 #${m.topic_id}`;
    if (m.reused) {
      host.append(el("div", { class: "mapping-row" }, [
        el("span", { text: "沿用旧主题" }),
        el("span", { class: "arrow", text: "→" }),
        el("b", { text: name }),
        el("span", { class: "sim muted", text: `质心相似度 ${fmtPct(m.similarity)} · ${m.members} 条` }),
      ]));
    } else {
      const threshold = state.config?.settings?.mapping_threshold ?? 0.5;
      const nearest = m.nearest_prev_name;
      let reason = `没有可对应的旧主题 · ${m.members} 条`;
      if (nearest) {
        reason = m.nearest_prev_sim >= threshold
          ? `最接近旧主题「${nearest}」（相似度 ${fmtPct(m.nearest_prev_sim)}），但该旧主题已被更相近的候选沿用；按新主题处理`
          : `最接近旧主题「${nearest}」，但相似度仅 ${fmtPct(m.nearest_prev_sim)}，未达对应阈值，按新主题处理`;
      }
      host.append(el("div", { class: "mapping-row new" }, [
        el("span", { text: `新主题「${name}」` }),
        el("span", { class: "arrow", text: "←" }),
        el("span", { text: reason }),
      ]));
    }
  }
  host.append(el("p", { class: "footnote", text:
    "对应关系按主题质心余弦相似度逐对匹配；簇编号只是本次训练的内部序号，相同编号不代表同一主题。" }));
}

async function refreshRunsQuiet(runId) {
  const history = await api("/api/history");
  state.history = history;
  return history.runs.find((r) => r.id === runId);
}

function renderRuns() {
  const host = $("#runs-list");
  host.textContent = "";
  const runs = state.history?.runs || [];
  if (!runs.length) {
    host.append(el("p", { class: "empty", text: "暂无运行记录。" }));
    return;
  }
  for (const run of runs.slice(0, 12)) {
    const s = run.summary || {};
    const bits = [];
    if (run.kind === "train") {
      bits.push(`候选主题 ${s.k ?? 0}`);
      if (s.new_topics !== undefined) bits.push(`新建 ${s.new_topics} · 沿用 ${s.reused_topics} · 归档 ${s.retired_topics}`);
    } else {
      bits.push(`匹配 ${s.matched ?? 0} · 待整理 ${s.pending ?? 0}`);
    }
    bits.push(`重复投递 ${s.duplicate_docs ?? 0}`);
    host.append(el("div", { class: "run-row" }, [
      el("div", {}, [
        el("span", { text: run.kind === "train" ? "训练" : "批次匹配" }),
        el("span", { class: "meta", text: ` · ${s.run_label || ""}` }),
      ]),
      el("div", { class: "meta", text: `${new Date(run.created_at).toLocaleString("zh-CN")} · ${bits.join(" · ")}` }),
    ]));
  }
}

// ---------------------------------------------------------------------------
// 规则
// ---------------------------------------------------------------------------

const RULE_LABELS = {
  strip_quote_headers: "剥离引用引导行（“在…写道：”）",
  strip_quoted_lines: "剥离 > 引用行",
  strip_reply_chain: "剥离回复链/转发原信",
  strip_signature: "剥离 -- 签名块",
  strip_signature_words: "剥离落款套话（此致/敬礼等）",
  strip_greeting: "剥离称呼语",
  strip_auto_footer: "剥离系统自动页脚",
  strip_case_noise: "剥离工单号/电话/邮箱等模板噪声",
};

function renderRules() {
  const config = state.config;
  const form = $("#rules-form");
  form.textContent = "";
  for (const [key, label] of Object.entries(RULE_LABELS)) {
    const input = el("input", { type: "checkbox", name: key });
    input.checked = !!config.rules[key];
    form.append(el("label", { class: "check" }, [input, label]));
  }
  const numFields = [
    ["min_chars", "短文本判定：清理后少于多少字进入待整理区", 0, 200, 1],
    ["near_duplicate_threshold", "重复投递判定阈值（0–1，越高越严格）", 0.5, 0.999, 0.01],
  ];
  for (const [key, label, min, max, step] of numFields) {
    const input = el("input", { type: "number", name: key, min, max, step });
    input.value = config.rules[key];
    form.append(el("label", { class: "check", style: "align-items:center;justify-content:space-between;gap:12px" }, [
      el("span", { text: label }), input,
    ]));
  }

  const settings = $("#settings-form");
  settings.textContent = "";
  const settingFields = [
    ["match_threshold", "新批次匹配阈值（相似度低于此值进入待整理区）", 0.05, 0.9, 0.01],
    ["mapping_threshold", "重训新旧主题对应阈值", 0.1, 0.9, 0.01],
    ["cluster_threshold", "聚类主阈值（越高主题越细）", 0.05, 0.3, 0.01],
    ["cluster_merge_threshold", "补合并阈值（仅合并两条以上的簇）", 0.02, 0.25, 0.01],
    ["outlier_threshold", "离群判定阈值", 0.01, 0.5, 0.01],
    ["max_topics", "主题数上限", 2, 20, 1],
  ];
  for (const [key, label, min, max, step] of settingFields) {
    const input = el("input", { type: "number", name: key, min, max, step });
    input.value = config.settings[key];
    settings.append(el("label", { class: "check", style: "align-items:center;justify-content:space-between;gap:12px" }, [
      el("span", { text: label }), input,
    ]));
  }

  const select = $("#sample-select");
  select.textContent = "";
  const samples = state.samples || [];
  for (const doc of samples) {
    select.append(el("option", { value: String(doc.id), text: `#${doc.id} ${doc.subject || "（无标题）"}` }));
  }
  renderSample();
}

async function renderSample() {
  const host = $("#sample-compare");
  const id = Number($("#sample-select").value || 1);
  host.textContent = "";
  let docs = state.docCache?.[id];
  if (!docs) {
    const { documents } = await api("/api/documents");
    state.docCache = Object.fromEntries(documents.map((d) => [d.id, d]));
    docs = state.docCache[id];
  }
  if (!docs) {
    host.append(el("p", { class: "empty", text: "没有这条工单。" }));
    return;
  }
  host.append(el("div", { class: "compare-col" }, [
    el("h3", { text: `原文（${docs.original_chars} 字）` }),
    el("div", { class: "seg-list" }, docs.segments.map((seg) => {
      const text = seg.text && seg.text.trim() ? seg.text : "（已删除的模板噪声）";
      const node = el("div", { class: `seg ${seg.kind}` }, [text]);
      const label = segKindLabel(seg.kind);
      if (label) node.append(el("span", { class: "tag", text: label }));
      return node;
    })),
  ]));
  host.append(el("div", { class: "compare-col" }, [
    el("h3", { text: `清理后（${docs.clean_text.length} 字，移除 ${docs.removed_chars} 字）` }),
    el("div", { class: "cleaned-box", text: docs.clean_text || "（空）" }),
  ]));
}

$("#sample-select")?.addEventListener("change", renderSample);

$("#btn-save-rules")?.addEventListener("click", async () => {
  const rules = {};
  for (const input of $$("#rules-form input")) {
    if (input.type === "checkbox") rules[input.name] = input.checked;
    else rules[input.name] = Number(input.value);
  }
  const retrain = $("#rules-retrain").checked;
  const btn = $("#btn-save-rules");
  btn.disabled = true;
  try {
    const res = await api("/api/rules", { method: "POST", body: JSON.stringify({ rules, retrain }) });
    toast(retrain ? "规则已保存并重新训练" : "规则已保存");
    state.docCache = null;
    await refreshAll();
    if (res.comparison) renderComparison(res.comparison, res.before_version, res.after_version);
  } catch (err) { toast(err.message, "error"); }
  btn.disabled = false;
});

$("#btn-save-settings")?.addEventListener("click", async () => {
  const body = {};
  for (const input of $$("#settings-form input")) body[input.name] = Number(input.value);
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    toast("阈值已保存，后续匹配与重训生效");
    await refreshAll();
  } catch (err) { toast(err.message, "error"); }
});

function renderComparison(comparison, aId, bId, target = "#rule-comparison") {
  const host = $(target);
  host.textContent = "";
  host.append(el("div", { class: "detail-section", style: "margin-top:0" }, [
    el("p", { class: "muted", text: `快照 #${aId} → #${bId}；完整版本可在「历史与回看」里打开。` }),
    el("div", { class: "table-host" }, comparisonTable(comparison)),
    el("h3", { class: "compare-sub", text: "按月趋势对照" }),
    el("p", { class: "muted", text: "只统计去重后的工单；重复投递不计入，避免一次转发被看成一次新增投诉。" }),
    el("div", { class: "table-host" }, monthComparisonTable(comparison)),
  ]));
}

function monthTotals(comparison, side) {
  const totals = new Map();
  for (const topic of comparison[side].topics) {
    for (const [month, count] of Object.entries(topic.months || {})) {
      totals.set(month, (totals.get(month) || 0) + count);
    }
  }
  return totals;
}

function monthComparisonTable(comparison) {
  const before = monthTotals(comparison, "a");
  const after = monthTotals(comparison, "b");
  const months = [...new Set([...before.keys(), ...after.keys()])].sort();
  const t = el("table", { class: "data" });
  t.append(el("thead", {}, el("tr", {}, [
    el("th", { text: "月份" }), el("th", { text: "修改前" }), el("th", { text: "修改后" }), el("th", { text: "变化" }),
  ])));
  const body = el("tbody");
  for (const month of months) {
    const a = before.get(month) || 0;
    const b = after.get(month) || 0;
    const diff = b - a;
    body.append(el("tr", {}, [
      el("td", { text: month }),
      el("td", { class: "num", text: String(a) }),
      el("td", { class: "num", text: String(b) }),
      el("td", { class: "num", text: diff === 0 ? "—" : `${diff > 0 ? "+" : ""}${diff}` }),
    ]));
  }
  t.append(body);
  return t;
}

function comparisonTable(comparison) {
  const t = el("table", { class: "data" });
  t.append(el("thead", {}, el("tr", {}, [
    el("th", { text: "主题" }), el("th", { text: "修改前" }), el("th", { text: "修改后" }), el("th", { text: "变化" }),
  ])));
  const mapA = new Map(comparison.a.topics.map((x) => [x.id, x]));
  const mapB = new Map(comparison.b.topics.map((x) => [x.id, x]));
  const ids = new Set([...mapA.keys(), ...mapB.keys()]);
  const body = el("tbody");
  for (const id of ids) {
    const a = mapA.get(id);
    const b = mapB.get(id);
    const name = (b || a)?.name || "未归类";
    const diff = ((b?.count || 0) - (a?.count || 0));
    body.append(el("tr", {}, [
      el("td", { text: name }),
      el("td", { class: "num", text: String(a?.count ?? 0) }),
      el("td", { class: "num", text: String(b?.count ?? 0) }),
      el("td", { class: "num", text: diff === 0 ? "—" : `${diff > 0 ? "+" : ""}${diff}` }),
    ]));
  }
  t.append(body);
  return t;
}

// ---------------------------------------------------------------------------
// 历史
// ---------------------------------------------------------------------------

function renderHistory() {
  const versions = state.history?.versions || [];
  const vHost = $("#version-list");
  vHost.textContent = "";
  if (!versions.length) vHost.append(el("p", { class: "empty", text: "还没有版本快照。" }));
  for (const v of versions.slice(0, 20)) {
    const row = el("div", { class: "version-row" }, [
      el("div", {}, [
        el("div", { text: `#${v.id} ${v.label}` }),
        el("div", { class: "meta", text: new Date(v.created_at).toLocaleString("zh-CN") }),
      ]),
      el("button", { class: "btn small ghost", text: "查看", onclick: () => openVersion(v.id) }),
    ]);
    vHost.append(row);
  }

  const aHost = $("#audit-list");
  aHost.textContent = "";
  const audit = state.history?.audit || [];
  if (!audit.length) aHost.append(el("p", { class: "empty", text: "还没有操作记录。" }));
  for (const a of audit.slice(0, 25)) {
    rowLog(aHost, a);
  }
  if (audit.length > 25) aHost.append(el("p", { class: "muted", text: `仅显示最近 25 条，共 ${audit.length} 条。` }));
}

function rowLog(host, a) {
  const detail = el("div", { class: "detail" });
  if (a.before && a.after) {
    detail.textContent = `${JSON.stringify(a.before)} → ${JSON.stringify(a.after)}`;
  } else if (a.after) {
    detail.textContent = JSON.stringify(a.after);
  }
  host.append(el("div", { class: "audit-row" }, [
    el("div", { class: "action", text: a.note || a.action }),
    el("div", { class: "meta", text: `${new Date(a.created_at).toLocaleString("zh-CN")} · ${a.entity} #${a.entity_id ?? "—"} · ${a.action}` }),
    detail,
  ]));
}

async function openVersion(versionId) {
  const version = await api(`/api/version?id=${versionId}`);
  const host = $("#version-view");
  host.hidden = false;
  host.textContent = "";
  const stateData = version.state;
  const topics = stateData.topics || [];
  const assignments = stateData.assignments || [];
  const counts = new Map();
  for (const a of assignments) {
    if (a.method === "duplicate") continue;
    const key = a.topic_id;
    if (!counts.has(key)) counts.set(key, { count: 0, months: {} });
    const slot = counts.get(key);
    slot.count += 1;
    const month = (a.submitted_at || "").slice(0, 7) || "未知";
    slot.months[month] = (slot.months[month] || 0) + 1;
  }
  const names = new Map(topics.map((t) => [t.id, t.name]));
  const max = Math.max(...[...counts.values()].map((v) => v.count), 1);
  host.append(el("div", { class: "version-detail-head" }, [
    el("div", {}, [
      el("h2", { text: `快照 #${version.id} · ${version.label}` }),
      el("p", { class: "muted", text: `${new Date(version.created_at).toLocaleString("zh-CN")} · ${assignments.length} 条归类记录` }),
    ]),
    el("button", { class: "btn small ghost", text: "关闭", onclick: () => { host.hidden = true; } }),
  ]));
  const list = el("div", { class: "version-topics" });
  const ordered = [...counts.entries()].sort((a, b) => b[1].count - a[1].count);
  for (const [topicId, info] of ordered) {
    list.append(el("div", { class: "version-topic-row" }, [
      el("span", { class: "name", text: names.get(topicId) || "未归类" }),
      el("span", { class: "bar", style: `width:${Math.max(4, (info.count / max) * 260)}px` }),
      el("span", { class: "num", text: `${info.count} 单` }),
      el("span", { class: "muted", text: Object.entries(info.months).map(([m, n]) => `${m} ${n}`).join(" · ") }),
    ]));
  }
  host.append(list);
  host.append(el("p", { class: "footnote", text: "这是当时保存的归类结果，不会随之后的改名、合并、重训改变。" }));

  // 与当前状态比较
  const current = state.topics;
  const currentCounts = new Map(current.map((t) => [t.id, t.canonical_count]));
  const rows = el("div", { class: "version-topics", style: "margin-top:12px" });
  rows.append(el("h3", { text: "与当前主题数量对比", style: "font-size:13px;color:var(--text-secondary)" }));
  for (const [topicId, info] of ordered.slice(0, 12)) {
    const now = currentCounts.get(topicId);
    rows.append(el("div", { class: "version-topic-row" }, [
      el("span", { class: "name", text: names.get(topicId) || "未归类" }),
      el("span", { class: "num", text: `当时 ${info.count}` }),
      el("span", { class: "muted", text: now === undefined ? "当前主题已合并/归档" : `当前 ${now}` }),
    ]));
  }
  host.append(rows);
  host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------------------------------------------------------------------------
// 弹窗与全局操作
// ---------------------------------------------------------------------------

function openDialog() {
  const backdrop = $("#dialog-backdrop");
  backdrop.hidden = false;
  document.body.style.overflow = "hidden";
  $("#dialog-close").focus({ preventScroll: true });
}

function closeDialog() {
  $("#dialog-backdrop").hidden = true;
  document.body.style.overflow = "";
}

$("#dialog-close").addEventListener("click", closeDialog);
$("#dialog-backdrop").addEventListener("click", (evt) => {
  if (evt.target === $("#dialog-backdrop")) closeDialog();
});
document.addEventListener("keydown", (evt) => {
  if (evt.key === "Escape" && !$("#dialog-backdrop").hidden) closeDialog();
});

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => {
      const active = t === tab;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", String(active));
    });
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${tab.dataset.tab}`));
    state.currentTab = tab.dataset.tab;
  });
});

$("#btn-theme").addEventListener("click", () => {
  const root = document.documentElement;
  const isDark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = isDark ? "light" : "dark";
  localStorage.setItem("ticket-theme", root.dataset.theme);
  renderTopics();
  renderOverallChart();
});
{
  const saved = localStorage.getItem("ticket-theme");
  if (saved) document.documentElement.dataset.theme = saved;
}

$("#btn-reset-demo")?.addEventListener("click", async () => {
  if (!confirm("将清空当前数据库并重新载入整套虚构演示工单，原有的改名、合并与历史快照都会丢失。继续？")) return;
  const btn = $("#btn-reset-demo");
  btn.disabled = true;
  try {
    await api("/api/demo/load", { method: "POST", body: JSON.stringify({ reset: true }) });
    toast("演示数据已重新载入");
    state.docCache = null;
    state.lastRunMapping = null;
    $("#retrain-card").hidden = true;
    await refreshAll();
  } catch (err) { toast(err.message, "error"); }
  btn.disabled = false;
});

$("#btn-reload").addEventListener("click", async () => {
  try {
    state.docCache = null;
    await refreshAll();
    toast("已重新载入");
  } catch (err) { toast(err.message, "error"); }
});

$("#btn-retrain").addEventListener("click", async () => {
  if (!confirm("重新训练会重新聚类全部工单，并按内容相似度对齐已确认主题；旧的归类会存成快照，可回看。继续？")) return;
  const btn = $("#btn-retrain");
  btn.disabled = true;
  try {
    const res = await api("/api/retrain", { method: "POST", body: JSON.stringify({}) });
    toast(`重新训练完成：新建 ${res.summary.new_topics} 个主题，沿用 ${res.summary.reused_topics} 个，归档 ${res.summary.retired_topics} 个`);
    state.docCache = null;
    await refreshAll();
    $$(".tab").find((t) => t.dataset.tab === "batches")?.click();
    if (res.comparison) {
      renderComparison(res.comparison, res.before_version, res.after_version, "#retrain-comparison");
      $("#retrain-card").hidden = false;
      $("#retrain-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  } catch (err) { toast(err.message, "error"); }
  btn.disabled = false;
});

refreshAll().catch((err) => {
  console.error(err);
  toast(`加载失败：${err.message}`, "error");
});
