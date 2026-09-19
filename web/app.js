/* web/app.js — 证券法律法规知识库前端逻辑（原生 JS，无外部依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
const API = {
  stats: () => fetch("/api/stats").then(r => r.json()),
  laws: (params) => fetch("/api/laws?" + new URLSearchParams(params)).then(r => r.json()),
  law: (id) => fetch(`/api/laws/${id}`).then(r => r.json()),
  relations: (id) => fetch(`/api/laws/${id}/relations`).then(r => r.json()),
  search: (q, top_k = 10) => fetch(`/api/search?q=${encodeURIComponent(q)}&top_k=${top_k}`).then(r => r.json()),
  chat: (messages) => fetch("/api/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  }).then(async r => r.ok ? r.json() : Promise.reject((await r.json()).detail)),
  wiki: (id, force = false) => fetch(`/api/laws/${id}/wiki?force=${force}`, { method: "POST" }).then(r => r.json()),
  getSettings: () => fetch("/api/settings").then(r => r.json()),
  putSettings: (body) => fetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(r => r.json()),
  testSettings: (body) => fetch("/api/settings/test", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(r => r.json()),
};

// ---------- 视图切换 ----------
const VIEWS = ["viewHome", "viewList", "viewDetail", "viewSearch"];
function showView(name) {
  VIEWS.forEach(v => $(v).classList.toggle("hidden", v !== name));
}

// ---------- 状态 ----------
let listState = { level: null, status: null, q: null, page: 1, pageSize: 20 };
let currentLawId = null;
let chatHistory = []; // [{role, content}]

// ---------- 初始化 ----------
async function init() {
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
  const data = await API.laws(params);
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
  const data = await API.law(id);
  const law = data.law;
  $("lawHeader").innerHTML = `
    <h2>${esc(law.title)}</h2>
    <div class="meta">
      <span class="badge level">${esc(law.level || "")}</span>
      ${statusBadge(law.status)}
      ${law.doc_number ? `<span>${esc(law.doc_number)}</span>` : ""}
      ${law.issuer ? `<span>${esc(law.issuer)}</span>` : ""}
      ${law.effective_date ? `<span>施行 ${esc(law.effective_date)}</span>` : ""}
      ${law.source_url ? `<a href="${esc(law.source_url)}" target="_blank">原文来源 ↗</a>` : ""}
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

async function loadRelations(id) {
  const box = $("relationList");
  box.innerHTML = '<span class="muted">加载中…</span>';
  try {
    const data = await API.relations(id);
    const items = [
      ...data.outgoing.map(r => ({ ...r, dir: "→" })),
      ...data.incoming.map(r => ({ ...r, dir: "←" })),
    ];
    if (!items.length) {
      box.innerHTML = '<span class="muted">暂无关联数据。可运行 python -m app.relations --law ' + id + ' 生成。</span>';
      return;
    }
    box.innerHTML = "";
    for (const r of items) {
      const div = document.createElement("div");
      div.className = "relation-item";
      const targetLawId = r.dir === "→" ? r.to_law_id : r.from_law_id;
      const targetTitle = r.dir === "→" ? r.to_law_title : r.from_law_title;
      div.innerHTML = `<span class="rel-type">${esc(r.rel_type)}</span>
        ${r.dir} <a data-law="${targetLawId}">${esc(targetTitle)}</a>
        <div class="muted">${esc(r.note || "")}</div>`;
      div.querySelector("a").onclick = (e) => {
        e.preventDefault();
        loadLawDetail(parseInt(e.target.dataset.law));
      };
      box.appendChild(div);
    }
  } catch (e) {
    box.innerHTML = `<span class="muted">加载失败</span>`;
  }
}

// ---------- 搜索 ----------
async function doSearch(q) {
  if (!q.trim()) return;
  showView("viewSearch");
  $("searchTitle").textContent = `搜索：${q}`;
  $("searchResults").innerHTML = '<div class="muted">检索中…</div>';
  const data = await API.search(q, 10);
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

// ---------- 对话 ----------
async function sendChat() {
  const input = $("chatInput");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  appendMsg("user", q);
  chatHistory.push({ role: "user", content: q });
  const aiDiv = appendMsg("ai", "研究中…（大模型正在检索知识库，可能需要数十秒）");
  $("btnSend").disabled = true;
  try {
    const r = await API.chat(chatHistory);
    let html = "";
    if (r.searches && r.searches.length) {
      html += `<div class="searches">已检索：${r.searches.map(esc).join("；")}</div>`;
    }
    html += renderInline(esc(r.content));
    if (r.citations && r.citations.length) {
      html += "<div>" + r.citations.map(c =>
        `<span class="cite-chip" data-law="${c.law_id}" data-no="${esc(c.article_no)}">📖 ${esc(c.title)} ${esc(c.article_no)}</span>`
      ).join("") + "</div>";
    }
    aiDiv.innerHTML = html;
    aiDiv.querySelectorAll(".cite-chip").forEach(chip => {
      chip.onclick = () => loadLawDetail(parseInt(chip.dataset.law), chip.dataset.no || null);
    });
    chatHistory.push({ role: "assistant", content: r.content });
  } catch (e) {
    aiDiv.innerHTML = `<span style="color:#cf222e">出错了：${esc(String(e))}</span>`;
  } finally {
    $("btnSend").disabled = false;
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
  const s = await API.getSettings();
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
  const body = {
    api_base: $("cfgApiBase").value.trim(),
    chat_model: $("cfgChatModel").value.trim(),
    embedding_model: $("cfgEmbedModel").value.trim(),
    temperature: parseFloat($("cfgTemp").value) || 0.1,
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
  const lines = md.split("\n");
  let html = "", inList = null;
  const flush = () => { if (inList) { html += `</${inList}>`; inList = null; } };
  for (let raw of lines) {
    const line = raw.trimEnd();
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

function renderInline(s) {
  return s
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/【([^】]+)】/g, '<span class="cite-chip">【$1】</span>');
}

// ---------- 工具 ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escReg(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

// ---------- 事件绑定 ----------
$("globalSearch").addEventListener("keydown", e => {
  if (e.key === "Enter") doSearch(e.target.value.trim());
});
$("btnSettings").onclick = openSettings;
$("btnCloseCfg").onclick = () => $("settingsModal").classList.add("hidden");
$("btnTestConn").onclick = async () => {
  setCfgStatus(false, "测试中…");
  const r = await API.testSettings(settingsBody());
  r.ok ? setCfgStatus(true, `✓ 连通成功：${r.reply}`) : setCfgStatus(false, `✗ ${r.error}`);
};
$("btnSaveCfg").onclick = async () => {
  setCfgStatus(false, "保存中…");
  const r = await API.putSettings(settingsBody());
  if (r.ok) setCfgStatus(true, "✓ 已保存到 data/config.json");
  else setCfgStatus(false, "保存失败");
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

init();
