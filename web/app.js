/* web/app.js — 证券法律法规知识库前端逻辑（原生 JS，无外部依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
// 统一响应处理：非 2xx 时抛出后端 detail（避免调用处拿 undefined 解构崩界面）
async function j(r) {
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    // FastAPI 422 的 detail 是数组，直接 String 会变 "[object Object]"
    const d = data && data.detail;
    const msg = Array.isArray(d) ? d.map(x => x.msg || JSON.stringify(x)).join("; ")
      : (typeof d === "string" ? d : JSON.stringify(d));
    throw msg && msg !== "undefined" ? msg : `HTTP ${r.status}`;
  }
  return data;
}
const API = {
  stats: () => fetch("/api/stats").then(j),
  laws: (params) => fetch("/api/laws?" + new URLSearchParams(params)).then(j),
  law: (id) => fetch(`/api/laws/${id}`).then(j),
  relations: (id) => fetch(`/api/laws/${id}/relations`).then(j),
  search: (q, top_k = 10) => fetch(`/api/search?q=${encodeURIComponent(q)}&top_k=${top_k}`).then(j),
  chat: (messages) => fetch("/api/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  }).then(j),
  wiki: (id, force = false) => fetch(`/api/laws/${id}/wiki?force=${force}`, { method: "POST" }).then(j),
  getSettings: () => fetch("/api/settings").then(j),
  putSettings: (body) => fetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(j),
  testSettings: (body) => fetch("/api/settings/test", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(j),
};

// ---------- 视图切换 ----------
const VIEWS = ["viewHome", "viewList", "viewDetail", "viewSearch", "viewGraph"];
function showView(name) {
  VIEWS.forEach(v => $(v).classList.toggle("hidden", v !== name));
}

// ---------- 状态 ----------
let listState = { level: null, status: null, q: null, page: 1, pageSize: 20 };
let currentLawId = null;
let chatHistory = []; // [{role, content}]

// ---------- 初始化 ----------
async function init() {
  try {
    const stats = await API.stats();
    $("statBadge").textContent = `${stats.laws} 部法规 · ${stats.articles} 条法条`;
    if (stats.last_sync) {
      $("syncInfo").textContent =
        `最近同步：${stats.last_sync.source_name} ${stats.last_sync.finished_at}（${stats.last_sync.status}）`;
    }
    // 左侧导航树
    const nav = $("levelNav");
    nav.innerHTML = "";
    for (const [level, count] of Object.entries(stats.by_level)) {
      const a = document.createElement("a");
      a.textContent = `▸ ${level}（${count}）`;
      a.onclick = () => {
        listState = { ...listState, level, status: null, q: null, page: 1 };
        nav.querySelectorAll("a").forEach(x => x.classList.remove("active"));
        a.classList.add("active");
        loadLawList(level);
      };
      nav.appendChild(a);
    }
    document.querySelectorAll("[data-status]").forEach(a => {
      a.onclick = (e) => {
        e.preventDefault();
        listState = { ...listState, status: a.dataset.status, level: null, q: null, page: 1 };
        loadLawList(`状态：${a.dataset.status}`);
      };
    });
  } catch (e) {
    $("statBadge").textContent = `加载失败：${esc(String(e))}`;
  }
}

// ---------- 法规列表 ----------
async function loadLawList(title) {
  showView("viewList");
  $("listTitle").textContent = title || "全部法规";
  const params = {
    page: listState.page, page_size: listState.pageSize,
  };
  if (listState.level) params.level = listState.level;
  if (listState.status) params.status = listState.status;
  if (listState.q) params.q = listState.q;
  let data;
  try {
    data = await API.laws(params);
  } catch (e) {
    $("lawList").innerHTML = `<div class="muted">加载失败：${esc(String(e))}</div>`;
    $("pager").innerHTML = "";
    return;
  }
  $("listCount").textContent = `共 ${data.total} 部`;
  const box = $("lawList");
  box.innerHTML = "";
  if (!data.items.length) {
    box.innerHTML = '<div class="muted">暂无数据。请先运行爬虫同步：python -m crawler.sync</div>';
  }
  for (const law of data.items) {
    box.appendChild(lawCard(law));
  }
  // 分页
  const pager = $("pager");
  pager.innerHTML = "";
  const totalPages = Math.max(1, Math.ceil(data.total / listState.pageSize));
  const prev = btn("上一页", listState.page > 1, () => { listState.page--; loadLawList(title); });
  const info = document.createElement("span");
  info.className = "muted";
  info.textContent = ` ${listState.page} / ${totalPages} `;
  const next = btn("下一页", listState.page < totalPages, () => { listState.page++; loadLawList(title); });
  pager.append(prev, info, next);
}

function lawCard(law) {
  const d = document.createElement("div");
  d.className = "law-card";
  const badge = statusBadge(law.status);
  d.innerHTML = `
    <div class="t">${esc(law.title)}</div>
    <div class="meta">
      <span class="badge level">${esc(law.level || "未分类")}</span>
      ${badge}
      ${law.issuer ? `<span>${esc(law.issuer)}</span>` : ""}
      ${law.issue_date ? `<span>发布 ${esc(law.issue_date)}</span>` : ""}
      ${law.effective_date ? `<span>施行 ${esc(law.effective_date)}</span>` : ""}
      <span>${esc(law.source_name || "")}</span>
    </div>`;
  d.onclick = () => loadLawDetail(law.id);
  return d;
}

function statusBadge(status) {
  const cls = status === "现行有效" ? "ok" : status === "已修订" ? "warn" : "dead";
  return `<span class="badge ${cls}">${esc(status || "未知")}</span>`;
}

function btn(text, enabled, onclick) {
  const b = document.createElement("button");
  b.textContent = text;
  b.disabled = !enabled;
  b.onclick = onclick;
  return b;
}

// ---------- 法规详情 ----------
async function loadLawDetail(id, highlightNo = null) {
  showView("viewDetail");
  currentLawId = id;
  let data;
  try {
    data = await API.law(id);
  } catch (e) {
    $("lawHeader").innerHTML = `<h2>加载失败</h2><div class="muted">${esc(String(e))}</div>`;
    $("articleList").innerHTML = "";
    $("wikiBox").innerHTML = "";
    $("relationList").innerHTML = "";
    return;
  }
  const law = data.law;
  $("lawHeader").innerHTML = `
    <h2>${esc(law.title)}</h2>
    <div class="meta">
      <span class="badge level">${esc(law.level || "")}</span>
      ${statusBadge(law.status)}
      ${law.doc_number ? `<span>${esc(law.doc_number)}</span>` : ""}
      ${law.issuer ? `<span>${esc(law.issuer)}</span>` : ""}
      ${law.effective_date ? `<span>施行 ${esc(law.effective_date)}</span>` : ""}
      ${law.source_url ? `<a href="${esc(safeUrl(law.source_url))}" target="_blank" rel="noopener noreferrer">原文来源 ↗</a>` : ""}
    </div>`;
  // 条文
  const list = $("articleList");
  list.innerHTML = "";
  for (const a of data.articles) {
    const item = document.createElement("div");
    item.className = "article-item" + (highlightNo && a.article_no === highlightNo ? " hit" : "");
    item.id = "art-" + a.id;
    item.innerHTML = `<span class="no">${esc(a.article_no || "全文")}</span>
      <div class="text">${esc(a.text)}</div>`;
    list.appendChild(item);
  }
  if (highlightNo) {
    setTimeout(() => {
      const el = list.querySelector(".article-item.hit");
      if (el) el.scrollIntoView({ block: "center" });
    }, 50);
  }
  // wiki 缓存
  const wikiBox = $("wikiBox");
  if (data.wiki) {
    wikiBox.innerHTML = renderMarkdown(data.wiki.markdown);
    wikiBox.dataset.cached = "1";
  } else {
    wikiBox.innerHTML = '<div class="placeholder">尚未生成。点击上方"生成/刷新解读"由大模型按需撰写（会消耗 token）。</div>';
    delete wikiBox.dataset.cached;
  }
  // 关联法规
  loadRelations(id);
}

// ---------- 关联法规（双向分组 + 类型筛选） ----------
let relItems = [];        // 当前法规的全部关联项
let relFilter = null;     // 选中的 rel_type（null = 全部）

async function loadRelations(id) {
  const box = $("relationList");
  box.innerHTML = '<span class="muted">加载中…</span>';
  relFilter = null;
  try {
    const data = await API.relations(id);
    relItems = [
      ...data.outgoing.map(r => ({ ...r, dir: "out" })),
      ...data.incoming.map(r => ({ ...r, dir: "in" })),
    ];
    // 头部徽章：关联总数
    const h3 = document.querySelector("#relationBox h3");
    if (h3) {
      h3.innerHTML = `关联法规 <span class="rel-count-badge">${relItems.length}</span>`;
    }
    renderRelations();
  } catch (e) {
    box.innerHTML = `<span class="muted">加载失败：${esc(String(e))}</span>`;
  }
}

function renderRelations() {
  const box = $("relationList");
  if (!relItems.length) {
    box.innerHTML = '<span class="muted">暂无关联数据。</span>';
    return;
  }
  // 类型筛选 chips（仅列出实际存在的类型）
  const types = [...new Set(relItems.map(r => r.rel_type))];
  let html = '<div class="rel-filter">' +
    `<span class="rf-chip ${relFilter === null ? "on" : ""}" data-t="">全部 ${relItems.length}</span>` +
    types.map(t => {
      const n = relItems.filter(r => r.rel_type === t).length;
      return `<span class="rf-chip ${relFilter === t ? "on" : ""}" data-t="${esc(t)}">${esc(t)} ${n}</span>`;
    }).join("") + "</div>";
  const shown = relItems.filter(r => !relFilter || r.rel_type === relFilter);
  for (const [dir, label] of [["out", "本法规 → 关联（出向）"], ["in", "被关联 → 本法规（入向）"]]) {
    const group = shown.filter(r => r.dir === dir);
    if (!group.length) continue;
    html += `<div class="rel-group-title">${label} · ${group.length}</div>`;
    html += group.map(r => {
      const targetLawId = dir === "out" ? r.to_law_id : r.from_law_id;
      const targetTitle = dir === "out" ? r.to_law_title : r.from_law_title;
      const arrow = dir === "out" ? "→" : "←";
      return `<div class="relation-item">
        <span class="rel-type">${esc(r.rel_type)}</span> ${arrow}
        <a data-law="${targetLawId}">${esc(targetTitle)}</a>
        <div class="muted">${esc(r.note || "")}</div></div>`;
    }).join("");
  }
  if (!shown.length) html += '<div class="muted">该类型暂无关联。</div>';
  box.innerHTML = html;
  box.querySelectorAll(".rf-chip").forEach(chip => {
    chip.onclick = () => {
      relFilter = chip.dataset.t || null;
      renderRelations();
    };
  });
  box.querySelectorAll("a[data-law]").forEach(a => {
    a.onclick = (e) => {
      e.preventDefault();
      loadLawDetail(parseInt(a.dataset.law));
    };
  });
}

// ---------- 搜索 ----------
async function doSearch(q) {
  if (!q.trim()) return;
  showView("viewSearch");
  $("searchTitle").textContent = `搜索：${q}`;
  $("searchResults").innerHTML = '<div class="muted">检索中…</div>';
  let data;
  try {
    data = await API.search(q, 10);
  } catch (e) {
    $("searchCount").textContent = "";
    $("searchResults").innerHTML = `<div class="muted">搜索失败：${esc(String(e))}</div>`;
    return;
  }
  $("searchCount").textContent = `${data.count} 条法条命中`;
  const box = $("searchResults");
  box.innerHTML = "";
  if (!data.results.length) {
    box.innerHTML = '<div class="muted">未命中。试试换关键词，或先同步更多法规入库。</div>';
    return;
  }
  for (const r of data.results) {
    const d = document.createElement("div");
    d.className = "search-hit";
    const frag = highlight(r.text, q);
    d.innerHTML = `<div class="loc">${esc(r.title)} · ${esc(r.article_no || "全文")}
      <span class="muted">（${esc(r.status)}｜相关度 ${r.score}）</span></div>
      <div class="frag">${frag}</div>`;
    d.onclick = () => loadLawDetail(r.law_id, r.article_no);
    box.appendChild(d);
  }
}

function highlight(text, q) {
  const escd = esc(text.length > 260 ? text.slice(0, 260) + "…" : text);
  // 对查询中 >=2 字的词片段高亮
  let out = escd;
  const words = q.split(/\s+/).filter(w => w.length >= 2);
  for (const w of words) {
    out = out.replace(new RegExp(escReg(w), "g"), (m) => `<mark>${m}</mark>`);
  }
  return out;
}

// ---------- 对话（悬浮面板） ----------
function chatPanelOpen() { return !$("chatPanel").classList.contains("hidden"); }

function toggleChat(open) {
  const panel = $("chatPanel");
  panel.classList.toggle("hidden", !open);
  if (open) {
    $("chatDot").classList.add("hidden");  // 打开即消红点
    const log = $("chatLog");
    log.scrollTop = log.scrollHeight;
    $("chatInput").focus();
  }
}

function initChatPanel() {
  $("chatFab").onclick = () => toggleChat(!chatPanelOpen());
  $("btnMinChat").onclick = () => toggleChat(false);

  // 标题栏拖动：记录位置到 localStorage（夹在视口内，防拖丢）
  const head = $("chatPanelHead"), panel = $("chatPanel");
  let drag = null;
  head.addEventListener("mousedown", e => {
    if (e.target.closest("button")) return;  // 最小化按钮不触发拖动
    const r = panel.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    e.preventDefault();
  });
  window.addEventListener("mousemove", e => {
    if (!drag) return;
    const w = panel.offsetWidth, h = panel.offsetHeight;
    const x = Math.min(Math.max(e.clientX - drag.dx, 0), innerWidth - w);
    const y = Math.min(Math.max(e.clientY - drag.dy, 0), innerHeight - h);
    panel.style.left = x + "px";
    panel.style.top = y + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    localStorage.setItem("chatPanelPos", JSON.stringify({ x, y }));
  });
  window.addEventListener("mouseup", () => { drag = null; });

  // 恢复上次拖动位置
  try {
    const pos = JSON.parse(localStorage.getItem("chatPanelPos") || "null");
    if (pos && pos.x >= 0 && pos.y >= 0) {
      const w = panel.offsetWidth || 400, h = panel.offsetHeight || 550;
      if (pos.x < innerWidth - 60 && pos.y < innerHeight - 60) {
        panel.style.left = Math.min(pos.x, innerWidth - w) + "px";
        panel.style.top = Math.min(pos.y, innerHeight - h) + "px";
        panel.style.right = "auto";
        panel.style.bottom = "auto";
      }
    }
  } catch (_) { /* 忽略损坏的位置记录 */ }
}

async function sendChat() {
  const input = $("chatInput");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  appendMsg("user", q);
  chatHistory.push({ role: "user", content: q });
  // 截断历史：避免长对话 token 成本线性增长（保留最近 40 轮上下文）
  if (chatHistory.length > 80) chatHistory = chatHistory.slice(-80);
  const aiDiv = appendMsg("ai", "研究中…（大模型正在检索知识库，可能需要数十秒）");
  $("btnSend").disabled = true;
  try {
    const r = await API.chat(chatHistory);
    let html = "";
    if (r.searches && r.searches.length) {
      html += `<div class="searches">已检索：${r.searches.map(esc).join("；")}</div>`;
    }
    html += renderInline(r.content);
    if (r.citations && r.citations.length) {
      html += "<div>" + r.citations.map(c =>
        `<span class="cite-chip" data-law="${c.law_id}" data-no="${esc(c.article_no)}">📖 ${esc(c.title)} ${esc(c.article_no)}</span>`
      ).join("") + "</div>";
    }
    aiDiv.innerHTML = html;
    aiDiv.querySelectorAll(".cite-chip").forEach(chip => {
      // 正文里由【】渲染出的装饰性 chip 没有 data-law，不可点击（否则 NaN → 422 → 界面卡死）
      if (!chip.dataset.law) return;
      chip.onclick = () => loadLawDetail(parseInt(chip.dataset.law), chip.dataset.no || null);
    });
    chatHistory.push({ role: "assistant", content: r.content });
  } catch (e) {
    aiDiv.innerHTML = `<span style="color:#cf222e">出错了：${esc(String(e))}</span>`;
    if (!chatPanelOpen()) $("chatDot").classList.remove("hidden");
  } finally {
    $("btnSend").disabled = false;
    // 面板收起时用红点提示"回复已就绪"
    if (!chatPanelOpen()) $("chatDot").classList.remove("hidden");
  }
}

function appendMsg(role, text) {
  const log = $("chatLog");
  if (role === "user" && log.querySelector(".chat-hint")) {
    log.querySelector(".chat-hint").remove();
  }
  const d = document.createElement("div");
  d.className = `msg ${role}`;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}

// ---------- wiki ----------
async function generateWiki(force) {
  if (!currentLawId) return;
  const box = $("wikiBox");
  box.innerHTML = '<div class="placeholder">生成中…（大模型撰写解读，约需 30-60 秒）</div>';
  try {
    const r = await API.wiki(currentLawId, force);
    if (r.error) {
      box.innerHTML = `<div class="placeholder">生成失败：${esc(r.error)}</div>`;
      return;
    }
    box.innerHTML = renderMarkdown(r.markdown);
  } catch (e) {
    box.innerHTML = `<div class="placeholder">生成失败：${esc(String(e))}</div>`;
  }
}

// ---------- 配置弹窗 ----------
async function openSettings() {
  let s;
  try {
    s = await API.getSettings();
  } catch (e) {
    alert(`读取配置失败：${String(e)}`);
    return;
  }
  $("cfgApiBase").value = s.api_base || "";
  $("cfgApiKey").value = "";
  $("cfgApiKey").placeholder = s.has_key ? `当前：${s.api_key_masked}（留空保留）` : "必填";
  $("cfgChatModel").value = s.chat_model || "";
  $("cfgEmbedModel").value = s.embedding_model || "";
  $("cfgTemp").value = s.temperature;
  $("cfgStatus").textContent = "";
  $("cfgStatus").className = "cfg-status";
  $("settingsModal").classList.remove("hidden");
}

function settingsBody(includeKey = true) {
  const temp = parseFloat($("cfgTemp").value);
  const body = {
    api_base: $("cfgApiBase").value.trim(),
    chat_model: $("cfgChatModel").value.trim(),
    embedding_model: $("cfgEmbedModel").value.trim(),
    temperature: Number.isFinite(temp) ? temp : 0.1,  // 0 是合法温度，不能被 || 吞掉
  };
  if (includeKey && $("cfgApiKey").value.trim()) body.api_key = $("cfgApiKey").value.trim();
  return body;
}

function setCfgStatus(ok, text) {
  $("cfgStatus").textContent = text;
  $("cfgStatus").className = "cfg-status " + (ok ? "ok" : "err");
}

// ---------- mini markdown 渲染 ----------
function renderMarkdown(md) {
  // 注意：不用 ES2020 的 ?? / ?. —— 兼容企业内网旧版浏览器（Chrome/Edge <80），
  // 语法错误是文件级的，一处不支持会挂掉整个 app.js
  const lines = String(md == null ? "" : md).split("\n");
  let html = "", inList = null;
  const flush = () => { if (inList) { html += `</${inList}>`; inList = null; } };
  for (let raw of lines) {
    const line = raw.replace(/\s+$/, "");   // trimEnd 是 ES2019，降级保旧浏览器
    if (/^###\s+/.test(line)) { flush(); html += `<h3>${renderInline(line.slice(4))}</h3>`; }
    else if (/^##\s+/.test(line)) { flush(); html += `<h2>${renderInline(line.slice(3))}</h2>`; }
    else if (/^#\s+/.test(line)) { flush(); html += `<h2>${renderInline(line.slice(2))}</h2>`; }
    else if (/^[-*]\s+/.test(line)) {
      if (inList !== "ul") { flush(); html += "<ul>"; inList = "ul"; }
      html += `<li>${renderInline(line.replace(/^[-*]\s+/, ""))}</li>`;
    } else if (/^\d+[.、]\s+/.test(line)) {
      if (inList !== "ol") { flush(); html += "<ol>"; inList = "ol"; }
      html += `<li>${renderInline(line.replace(/^\d+[.、]\s+/, ""))}</li>`;
    } else if (!line.trim()) { flush(); }
    else { flush(); html += `<p>${renderInline(line)}</p>`; }
  }
  flush();
  return html;
}

// 行内渲染自带 HTML 转义：renderInline 是"安全渲染"入口，调用方无需先 esc
// （wiki/聊天内容来自 LLM 对爬取正文的加工，被投毒页面可诱导输出 <img onerror> 等，
//  历史上 wiki 路径漏 esc 造成存储型 XSS）
function renderInline(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/【([^】]+)】/g, '<span class="cite-chip">【$1】</span>');
}

// ---------- 工具 ----------
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escReg(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
// source_url 来自爬取页面的属性，可能是被投毒的 javascript: 伪协议，仅放行 http(s)
function safeUrl(u) {
  return /^https?:\/\//i.test(String(u || "")) ? u : "#";
}

// ---------- 事件绑定 ----------
$("globalSearch").addEventListener("keydown", e => {
  if (e.key === "Enter") doSearch(e.target.value.trim());
});
$("btnSettings").onclick = openSettings;
$("btnCloseCfg").onclick = () => $("settingsModal").classList.add("hidden");
$("btnTestConn").onclick = async () => {
  setCfgStatus(false, "测试中…");
  try {
    const r = await API.testSettings(settingsBody());
    r.ok ? setCfgStatus(true, `✓ 连通成功：${r.reply}`) : setCfgStatus(false, `✗ ${r.error}`);
  } catch (e) {
    setCfgStatus(false, `✗ 请求失败：${String(e)}`);
  }
};
$("btnSaveCfg").onclick = async () => {
  setCfgStatus(false, "保存中…");
  try {
    const r = await API.putSettings(settingsBody());
    if (r.ok) setCfgStatus(true, "✓ 已保存到 data/config.json");
    else setCfgStatus(false, "保存失败");
  } catch (e) {
    setCfgStatus(false, `✗ 保存失败：${String(e)}`);
  }
};
$("btnSend").onclick = sendChat;
$("chatInput").addEventListener("keydown", e => {
  if (e.key === "Enter") sendChat();
});
$("btnClearChat").onclick = () => {
  chatHistory = [];
  const log = $("chatLog");
  log.innerHTML = '<div class="chat-hint">对话已清空。</div>';
};
$("btnBack").onclick = () => showView("viewList");
$("btnWiki").onclick = () => generateWiki(true);

initChatPanel();
init();
