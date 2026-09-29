/* web/graph.js — 知识图谱全景视图：径向分层布局 + Canvas 交互渲染（无外部依赖）
 * 数据源 GET /api/graph（法规级节点 + 去重边）。
 * 布局：效力级别按同心环带排布（法律居中 → 规范性文件最外），环带内均匀角度
 *       + 伪随机半径微扰，确定性（刷新位置不变）。
 * 交互：滚轮缩放（以鼠标为中心）、拖拽平移、悬停 tooltip + 邻边高亮、
 *       点击节点进入法规详情、点击图例聚焦/淡化层级。
 * 配色：6 效力级别分类色已过 dataviz 色板校验（CVD 分离/亮度带 PASS；
 *       对表面对比度不足的色由图例、tooltip、2px 表面描边救济）。 */
"use strict";

const GRAPH_LEVELS = ["法律", "行政法规", "部门规章", "司法解释", "自律规则", "规范性文件"];
const GRAPH_COLORS = {
  "法律": "#2a78d6", "行政法规": "#eb6834", "部门规章": "#1baf7a",
  "司法解释": "#eda100", "自律规则": "#e87ba4", "规范性文件": "#008300",
  "未分类": "#6a737d",
};
const GRAPH_SURFACE = "#ffffff";

let G = null;                     // {nodes, edges, byId, adj}
let gActive = null;               // 当前视图数据 {nodes, edges}（全部或某板块子图）
let gBoard = null;                // 当前业务板块（null = 全景）
let gView = { x: 0, y: 0, k: 1 }; // 平移 + 缩放
let gHover = null;                // 悬停节点 id
let gFocusLevel = null;           // 图例聚焦的层级（null = 全部）
let gCanvas = null, gCtx = null;

function levelColor(level) {
  return GRAPH_COLORS[level] || GRAPH_COLORS["未分类"];
}

function levelOrder(level) {
  const i = GRAPH_LEVELS.indexOf(level);
  return i >= 0 ? i : GRAPH_LEVELS.length;  // 未分类垫底（最外）
}

/* 确定性伪随机（同一 id 每次刷新结果一致，保证布局稳定） */
function rand01(seed) {
  const x = Math.sin(seed * 12.9898 + 78.233) * 43758.5453;
  return x - Math.floor(x);
}

/* 径向分层布局：每级一个环带 [rIn, rOut]，带内均匀角度 + 微扰 */
function layoutGraph(nodes) {
  const byLevel = new Map();
  for (const n of nodes) {
    const lv = levelOrder(n.level);
    if (!byLevel.has(lv)) byLevel.set(lv, []);
    byLevel.get(lv).push(n);
  }
  // 环带半径：保证带内节点最小角间距（周长 ≥ 数量 × 最小间距）
  const MIN_SPACING = 26;         // 逻辑像素
  const BAND_GAP = 46;            // 环带之间的空隙
  const CENTER_R = 150;           // 中心核心区半径
  let r = CENTER_R;
  const bands = [];
  const orderList = [...byLevel.keys()].sort((a, b) => a - b);
  for (const lv of orderList) {
    const arr = byLevel.get(lv);
    // 同层内按度数降序排（度数高的先占位，分布更均匀）
    arr.sort((a, b) => (b.out_deg + b.in_deg) - (a.out_deg + a.in_deg));
    const needR = arr.length * MIN_SPACING / (2 * Math.PI);
    const rIn = Math.max(r, needR * 0.82);
    const rOut = rIn + Math.max(64, needR * 0.34);
    bands.push({ lv, rIn, rOut });
    r = rOut + BAND_GAP;
  }
  const maxR = r;
  for (const band of bands) {
    const arr = byLevel.get(band.lv);
    arr.forEach((n, i) => {
      const a = (i / arr.length) * Math.PI * 2 + rand01(n.id) * 0.5;
      const rr = band.rIn + (band.rOut - band.rIn) * rand01(n.id + 7);
      n.x = Math.cos(a) * rr;
      n.y = Math.sin(a) * rr;
    });
  }
  return maxR;
}

async function showGraph() {
  showView("viewGraph");
  if (G) { drawGraph(); return; }  // 已加载过：只重绘
  const meta = $("graphMeta");
  meta.textContent = "加载图谱数据…";
  try {
    const data = await fetch("/api/graph").then(r => {
      if (!r.ok) throw `HTTP ${r.status}`;
      return r.json();
    });
    G = {
      nodes: data.nodes, edges: data.edges,
      byId: new Map(data.nodes.map(n => [n.id, n])),
      adj: new Map(),   // law_id -> Set(邻接 law_id)
    };
    for (const n of G.nodes) G.adj.set(n.id, new Set());
    for (const e of G.edges) {
      if (G.adj.has(e.s)) G.adj.get(e.s).add(e.t);
      if (G.adj.has(e.t)) G.adj.get(e.t).add(e.s);
    }
    gBoard = null;
    gActive = { nodes: G.nodes, edges: G.edges };
    gMaxR = layoutGraph(gActive.nodes);
    setupCanvas();
    renderLegend();
    renderBoards();
    updateGraphMeta();
    drawGraph();
  } catch (e) {
    meta.textContent = `图谱加载失败：${String(e)}`;
  }
}

/* ---------- 业务板块子图 ---------- */

function switchBoard(board) {
  gBoard = board || null;
  if (!gBoard) {
    gActive = { nodes: G.nodes, edges: G.edges };
  } else {
    const ids = new Set(G.nodes.filter(n => n.board === gBoard).map(n => n.id));
    gActive = {
      nodes: G.nodes.filter(n => n.board === gBoard),
      edges: G.edges.filter(e => ids.has(e.s) && ids.has(e.t)),  // 只画板块内边
    };
  }
  gHover = null;
  $("graphTip").classList.add("hidden");
  gMaxR = layoutGraph(gActive.nodes);   // 子图重新径向分层布局
  fitView();
  renderBoards();
  updateGraphMeta();
  drawGraph();
}

function renderBoards() {
  const box = $("graphBoards");
  const counts = new Map();
  for (const n of G.nodes) counts.set(n.board || "其他", (counts.get(n.board || "其他") || 0) + 1);
  const entries = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  box.innerHTML =
    `<span class="gb-chip ${gBoard == null ? "on" : ""}" data-b="">全景 ${G.nodes.length}</span>` +
    entries.map(([b, c]) =>
      `<span class="gb-chip ${gBoard === b ? "on" : ""}" data-b="${esc(b)}">${esc(b)} ${c}</span>`
    ).join("");
  box.querySelectorAll(".gb-chip").forEach(chip => {
    chip.onclick = () => switchBoard(chip.dataset.b || null);
  });
}

function updateGraphMeta() {
  const meta = $("graphMeta");
  if (gBoard) {
    meta.textContent = `${gBoard} · ${gActive.nodes.length} 部法规 · ${gActive.edges.length} 条板块内关联 · 半径 ∝ 关联数`;
  } else {
    meta.textContent = `${G.nodes.length} 部法规 · ${G.edges.length} 条关联 · 半径 ∝ 关联数 · 点击上方板块查看子图`;
  }
}

function fitView() {
  const w = gCanvas.clientWidth, h = gCanvas.clientHeight;
  gView.k = Math.min(w, h) / (gMaxR * 2.15);
  gView.x = w / 2; gView.y = h / 2;
}

/* ---------- Canvas 初始化与坐标变换 ---------- */
let gMaxR = 1000;

function setupCanvas() {
  gCanvas = document.getElementById("graphCanvas");
  gCtx = gCanvas.getContext("2d");
  resizeCanvas();
  window.addEventListener("resize", () => { resizeCanvas(); drawGraph(); });

  fitView();   // 初始视图：fit to screen
  bindCanvasEvents();
}

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  gCanvas.width = gCanvas.clientWidth * dpr;
  gCanvas.height = gCanvas.clientHeight * dpr;
  gCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

/* 逻辑坐标 → 屏幕坐标 */
function toScreen(n) {
  return { x: gView.x + n.x * gView.k, y: gView.y + n.y * gView.k };
}
/* 屏幕坐标 → 逻辑坐标 */
function toWorld(sx, sy) {
  return { x: (sx - gView.x) / gView.k, y: (sy - gView.y) / gView.k };
}

function nodeRadius(n) {
  const deg = n.out_deg + n.in_deg;
  return 2.6 + Math.sqrt(deg) * 0.9;   // 半径 ∝ √度数
}

function hitNode(sx, sy) {
  let best = null, bestD = 1e9;
  for (const n of gActive.nodes) {
    const p = toScreen(n);
    const d = Math.hypot(p.x - sx, p.y - sy);
    const r = nodeRadius(n) * gView.k + 4;
    if (d <= r && d < bestD) { best = n; bestD = d; }
  }
  return best;
}

/* ---------- 渲染 ---------- */
function drawGraph() {
  const w = gCanvas.clientWidth, h = gCanvas.clientHeight;
  gCtx.clearRect(0, 0, w, h);
  gCtx.fillStyle = GRAPH_SURFACE;
  gCtx.fillRect(0, 0, w, h);

  if (!G || !gActive) return;
  const hovered = gHover != null ? G.byId.get(gHover) : null;
  const hoverAdj = hovered ? G.adj.get(hovered.id) : null;
  const dimmed = gFocusLevel != null;
  // 子图模式的关键可读性改进：小板块（≤60 节点，fit 后间距足以容纳）
  // 全部直标；大板块仍用 top45 + 缩放阈值
  const allLabels = gBoard != null && gActive.nodes.length <= 60;

  // 边：低 alpha 叠加；悬停节点的邻边高亮
  for (const e of gActive.edges) {
    const a = G.byId.get(e.s), b = G.byId.get(e.t);
    if (!a || !b) continue;
    const isAdj = hoverAdj && (e.s === hovered.id || e.t === hovered.id);
    const inFocus = !dimmed || (a.level === gFocusLevel && b.level === gFocusLevel);
    gCtx.strokeStyle = isAdj ? "rgba(26,75,140,0.55)"
      : `rgba(120,130,145,${inFocus ? 0.10 : 0.035})`;
    gCtx.lineWidth = isAdj ? 1.4 : 0.7;
    const pa = toScreen(a), pb = toScreen(b);
    gCtx.beginPath();
    gCtx.moveTo(pa.x, pa.y);
    gCtx.lineTo(pb.x, pb.y);
    gCtx.stroke();
  }

  // 节点：2px 表面描边（规范要求），聚焦模式下非该层淡化
  for (const n of gActive.nodes) {
    const p = toScreen(n);
    if (p.x < -20 || p.y < -20 || p.x > w + 20 || p.y > h + 20) continue;
    const r = nodeRadius(n) * gView.k;
    if (r < 0.4) continue;
    const faded = dimmed && n.level !== gFocusLevel;
    const isHover = hovered && n.id === hovered.id;
    gCtx.globalAlpha = faded ? 0.16 : 1;
    gCtx.beginPath();
    gCtx.arc(p.x, p.y, Math.max(r, 1.2), 0, Math.PI * 2);
    gCtx.fillStyle = levelColor(n.level);
    gCtx.fill();
    gCtx.lineWidth = isHover ? 2.4 : 1.2;
    gCtx.strokeStyle = isHover ? "#24292f" : GRAPH_SURFACE;
    gCtx.stroke();
    gCtx.globalAlpha = 1;
  }

  // 标签：悬停必显 + 全景模式 top45 常显 + 子图模式（≤170 节点）全部直标
  gCtx.font = "11px 'Segoe UI', 'Microsoft YaHei', sans-serif";
  gCtx.textAlign = "center";
  gCtx.textBaseline = "top";
  const minDegForLabel = gView.k > 1.6 ? 8 : gView.k > 0.9 ? 20 : 1e9;
  for (const n of gActive.nodes) {
    const deg = n.out_deg + n.in_deg;
    const isHover = hovered && n.id === hovered.id;
    if (!isHover && !allLabels && deg < minDegForLabel && deg < 45) continue;
    if (gFocusLevel != null && n.level !== gFocusLevel && !isHover) continue;
    const p = toScreen(n);
    const r = nodeRadius(n) * gView.k;
    const label = n.title.length > 16 ? n.title.slice(0, 15) + "…" : n.title;
    gCtx.fillStyle = "rgba(255,255,255,0.82)";
    const tw = gCtx.measureText(label).width;
    gCtx.fillRect(p.x - tw / 2 - 3, p.y + r + 2, tw + 6, 14);
    gCtx.fillStyle = "#24292f";
    gCtx.fillText(label, p.x, p.y + r + 3);
  }
}

/* ---------- 图例 ---------- */
function renderLegend() {
  const box = document.getElementById("graphLegend");
  const counts = new Map();
  for (const n of G.nodes) counts.set(n.level, (counts.get(n.level) || 0) + 1);
  const entries = [...counts.entries()].sort(
    (a, b) => levelOrder(a[0]) - levelOrder(b[0]));
  box.innerHTML = entries.map(([lv, c]) =>
    `<div class="lg-item ${gFocusLevel != null && lv !== gFocusLevel ? "dim" : ""}" data-lv="${esc(lv)}">
       <span class="lg-swatch" style="background:${levelColor(lv)}"></span>
       ${esc(lv)} <span class="lg-count">${c}</span></div>`).join("");
  box.querySelectorAll(".lg-item").forEach(item => {
    item.onclick = () => {
      const lv = item.dataset.lv;
      gFocusLevel = (gFocusLevel === lv) ? null : lv;
      renderLegend();
      drawGraph();
    };
  });
}

/* ---------- 事件绑定 ---------- */
function bindCanvasEvents() {
  let panning = null, moved = false;

  gCanvas.addEventListener("wheel", e => {
    e.preventDefault();
    const rect = gCanvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    const before = toWorld(sx, sy);
    gView.k = Math.min(6, Math.max(0.12, gView.k * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    const after = toWorld(sx, sy);
    gView.x += (after.x - before.x) * gView.k;
    gView.y += (after.y - before.y) * gView.k;
    drawGraph();
  }, { passive: false });

  gCanvas.addEventListener("mousedown", e => {
    panning = { sx: e.clientX, sy: e.clientY, vx: gView.x, vy: gView.y };
    moved = false;
    gCanvas.classList.add("dragging");
  });
  window.addEventListener("mousemove", e => {
    if (panning) {
      const dx = e.clientX - panning.sx, dy = e.clientY - panning.sy;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      gView.x = panning.vx + dx;
      gView.y = panning.vy + dy;
      drawGraph();
    }
  });
  window.addEventListener("mouseup", () => {
    panning = null;
    gCanvas.classList.remove("dragging");
  });

  gCanvas.addEventListener("mousemove", e => {
    if (panning) return;
    const rect = gCanvas.getBoundingClientRect();
    const n = hitNode(e.clientX - rect.left, e.clientY - rect.top);
    const tip = document.getElementById("graphTip");
    if (n) {
      gHover = n.id;
      const deg = n.out_deg + n.in_deg;
      tip.innerHTML = `<b>${esc(n.title)}</b><br>${esc(n.level)} · 关联 ${deg} 部` +
        (n.status ? ` · ${esc(n.status)}` : "");
      tip.classList.remove("hidden");
      const wrap = gCanvas.parentElement.getBoundingClientRect();
      let tx = e.clientX - wrap.left + 14, ty = e.clientY - wrap.top + 14;
      if (tx + 260 > wrap.width) tx -= 280;
      if (ty + 70 > wrap.height) ty -= 84;
      tip.style.left = tx + "px";
      tip.style.top = ty + "px";
      gCanvas.style.cursor = "pointer";
      drawGraph();
    } else if (gHover != null) {
      gHover = null;
      tip.classList.add("hidden");
      gCanvas.style.cursor = "grab";
      drawGraph();
    }
  });

  gCanvas.addEventListener("mouseleave", () => {
    gHover = null;
    document.getElementById("graphTip").classList.add("hidden");
    drawGraph();
  });

  gCanvas.addEventListener("click", e => {
    if (moved) return;  // 拖拽后的 mouseup 不触发跳转
    const rect = gCanvas.getBoundingClientRect();
    const n = hitNode(e.clientX - rect.left, e.clientY - rect.top);
    if (n && typeof loadLawDetail === "function") {
      document.getElementById("graphTip").classList.add("hidden");
      loadLawDetail(n.id);   // app.js 的全局函数：进入法规详情
    }
  });
}

/* 导航入口（graph.js 自包含，不依赖 app.js 加载顺序） */
document.getElementById("navGraph").addEventListener("click", e => {
  e.preventDefault();
  showGraph();
});
