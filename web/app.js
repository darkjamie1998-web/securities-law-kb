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
function queryString(params) {
  const query = new URLSearchParams();
  Object.keys(params || {}).forEach(key => {
    const value = params[key];
    if (value !== null && value !== undefined && String(value) !== "") {
      query.set(key, String(value));
    }
  });
  return query.toString();
}

const API = {
  stats: () => fetch("/api/stats").then(j),
  laws: (params) => fetch("/api/laws?" + queryString(params)).then(j),
  lawReader: (id, params) => fetch(`/api/laws/${id}?` + queryString(
    Object.assign({ mode: "reader" }, params || {}))).then(j),
  lawArticles: (id, params) => fetch(`/api/laws/${id}/articles?` + queryString(params)).then(j),
  relations: (id) => fetch(`/api/laws/${id}/relations`).then(j),
  search: (params) => fetch("/api/search?" + queryString(params)).then(j),
  chat: (messages, law_id = null, signal) => fetch("/api/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, law_id }), signal: signal,
  }).then(j),
  // 原始 Response 交给 NDJSON 读取器；不能在这里用 j() 提前消费 body。
  chatStream: (messages, law_id = null, signal) => fetch("/api/chat/stream", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, law_id }), signal: signal,
  }),
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

// ---------- Hash 路由与共享状态 ----------
const VIEWS = ["viewHome", "viewList", "viewDetail", "viewSearch", "viewGraph"];
const LAW_PAGE_SIZE = 6;
const ARTICLE_PAGE_SIZE = 10;
const READER_PAGE_SIZE = 50;
let currentLawId = null;
let chatHistory = [];
let appStats = null;
let statsPromise = null;
let routeSerial = 0;
let lastHash = null;
let navigationIntent = null;
const scrollPositions = {};

function showView(name) {
  VIEWS.forEach(v => $(v).classList.toggle("hidden", v !== name));
}

function makeHash(path, params) {
  const query = queryString(params);
  return "#" + path + (query ? "?" + query : "");
}

function paramsObject(params) {
  const out = {};
  if (!params) return out;
  params.forEach((value, key) => { out[key] = value; });
  return out;
}

function positivePage(value) {
  const page = parseInt(value || "1", 10);
  return Number.isFinite(page) && page > 0 ? page : 1;
}

function parseRoute() {
  const raw = (location.hash || "#/home").slice(1);
  const qIndex = raw.indexOf("?");
  const path = (qIndex >= 0 ? raw.slice(0, qIndex) : raw) || "/home";
  const params = new URLSearchParams(qIndex >= 0 ? raw.slice(qIndex + 1) : "");
  if (path === "/home") return { name: "home", path: path, params: params };
  if (path === "/laws") return { name: "laws", path: path, params: params };
  if (path === "/search") {
    let scope = params.get("scope") || "all";
    if (["all", "law", "article"].indexOf(scope) < 0) scope = "all";
    return { name: "search", path: path, params: params, scope: scope };
  }
  if (path === "/graph") return { name: "graph", path: path, params: params };
  const lawMatch = path.match(/^\/law\/([^/]+)$/);
  if (lawMatch) {
    const valid = /^[1-9]\d*$/.test(lawMatch[1]);
    return {
      name: "law", path: path, params: params,
      id: valid ? parseInt(lawMatch[1], 10) : null,
    };
  }
  history.replaceState(null, "", "#/home");
  return { name: "home", path: "/home", params: new URLSearchParams() };
}

function navigate(path) {
  const hash = path.charAt(0) === "#" ? path : makeHash(path);
  navigationIntent = hash;
  if ((location.hash || "#/home") === hash) handleRoute();
  else location.hash = hash;
}

function handleInternalLinkClick(e) {
  if (e.defaultPrevented || e.button !== 0 ||
      e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  const target = e.target;
  const anchor = target && target.closest ? target.closest("a[href^='#/']") : null;
  if (!anchor || (anchor.target && anchor.target.toLowerCase() !== "_self") ||
      anchor.hasAttribute("download")) return;
  const hash = anchor.getAttribute("href");
  navigationIntent = hash;
  if ((location.hash || "#/home") === hash) {
    e.preventDefault();
    handleRoute();
  }
}

function navigateWith(route, changes) {
  const params = paramsObject(route.params);
  Object.keys(changes).forEach(key => {
    const value = changes[key];
    if (value === null || value === "" || value === undefined) delete params[key];
    else params[key] = value;
  });
  navigate(makeHash(route.path, params));
}

const routeHandlers = {
  home: (route, ticket) => loadHome(route, ticket),
  laws: (route, ticket) => loadLawList(route, ticket),
  search: (route, ticket) => loadUnifiedSearch(route, ticket),
  law: (route, ticket) => loadLawDetail(route, ticket),
  graph: (route, ticket) => {
    if (typeof showGraph !== "function") throw new Error("图谱模块尚未加载");
    return showGraph(route, ticket);
  },
};

function registerRouteHandler(name, handler) {
  routeHandlers[name] = handler;
}

function updateActiveNav(route) {
  document.querySelectorAll(".level-nav a").forEach(a => a.classList.remove("active"));
  if (route.name === "home") $("navHome").classList.add("active");
  if (route.name === "graph") $("navGraph").classList.add("active");
  if (route.name === "laws") {
    const level = route.params.get("level"), status = route.params.get("status");
    if (!level && !status) $("navLaws").classList.add("active");
    document.querySelectorAll("#levelNav a, #statusNav a").forEach(a => {
      const link = new URLSearchParams((a.getAttribute("href").split("?")[1] || ""));
      if ((level && link.get("level") === level) || (status && link.get("status") === status)) {
        a.classList.add("active");
      }
    });
  }
  if (innerWidth <= 760) {
    $("sidebar").classList.add("collapsed");
    $("btnNavToggle").setAttribute("aria-expanded", "false");
  }
}

function shouldFocusMainAfterRoute() {
  if (!$("settingsModal").classList.contains("hidden") || evidenceIsOpen()) return false;
  const active = document.activeElement;
  if (chatPanelOpen() && $("chatPanel").contains(active)) return false;
  if (active && active !== $("main") &&
      active.matches("input, textarea, select, [contenteditable='true']")) return false;
  return true;
}

async function handleRoute() {
  closeReaderSide();
  closeEvidence(false);
  const route = parseRoute();
  updateChatScope(route);
  const hash = location.hash || "#/home";
  const isInitial = lastHash === null;
  const isPush = navigationIntent === hash;
  navigationIntent = null;
  if (lastHash && lastHash !== hash) scrollPositions[lastHash] = $("main").scrollTop;
  if (isPush) delete scrollPositions[hash];
  lastHash = hash;
  route.restoreScroll = !isInitial && !isPush && Object.prototype.hasOwnProperty.call(scrollPositions, hash);
  const ticket = ++routeSerial;
  updateActiveNav(route);
  try {
    await routeHandlers[route.name](route, ticket);
  } catch (e) {
    if (ticket === routeSerial) {
      showView(route.name === "law" ? "viewDetail" : "viewHome");
      const target = route.name === "law" ? $("lawHeader") : $("homeTrust");
      target.innerHTML = `<div class="error-box">页面加载失败：${esc(String(e))}</div>`;
    }
  }
  if (ticket === routeSerial) {
    requestAnimationFrame(() => {
      if (ticket !== routeSerial) return;
      const main = $("main"), top = scrollPositions[hash] || 0;
      main.scrollTop = top;
      if (!shouldFocusMainAfterRoute()) return;
      try { main.focus({ preventScroll: true }); }
      catch (_) { main.focus(); }
      main.scrollTop = top;
    });
  }
}

function ensureStats() {
  if (appStats) return Promise.resolve(appStats);
  if (!statsPromise) {
    statsPromise = API.stats().then(stats => {
      appStats = stats;
      updateStatsUI(stats);
      return stats;
    }).catch(e => {
      statsPromise = null;
      throw e;
    });
  }
  return statsPromise;
}

function updateStatsUI(stats) {
  $("statBadge").textContent = `${stats.laws} 部法规 · ${stats.articles} 条法条`;
  const sync = stats.last_success_sync;
  $("syncInfo").textContent = sync
    ? `最近成功同步：${sync.finished_at || "时间未记录"}`
    : "尚无成功同步记录";
  const nav = $("levelNav");
  nav.innerHTML = "";
  Object.keys(stats.by_level || {}).forEach(level => {
    const a = document.createElement("a");
    a.href = makeHash("/laws", { level: level });
    a.textContent = `▸ ${level}（${stats.by_level[level]}）`;
    nav.appendChild(a);
  });
  updateActiveNav(parseRoute());
}

// ---------- 首页 ----------
function readRecentViews() {
  try {
    const value = JSON.parse(localStorage.getItem("recentLaws") || "[]");
    if (!Array.isArray(value)) return [];
    return value.filter(item => item && Number.isInteger(item.id) && item.id > 0 &&
      typeof item.title === "string" && item.title && Number.isFinite(item.time)
    ).slice(0, 6);
  } catch (_) {
    safeStorageRemove("recentLaws");
    return [];
  }
}

function recordRecentLaw(id, title) {
  try {
    const items = readRecentViews().filter(item => item.id !== id);
    items.unshift({ id: id, title: title, time: Date.now() });
    safeStorageSet("recentLaws", JSON.stringify(items.slice(0, 6)));
  } catch (_) { /* 隐私模式或存储已满不影响阅读 */ }
}

function renderRecentViews() {
  const box = $("homeRecentViews"), items = readRecentViews();
  if (!items.length) {
    box.innerHTML = '<div class="empty-compact">暂无浏览记录。打开法规后，最近 6 部会显示在这里。</div>';
    return;
  }
  box.innerHTML = items.map(item => {
    const date = new Date(item.time);
    return `<a class="home-law-row" href="${esc(makeHash("/law/" + item.id))}">
      <span class="home-law-title">${esc(item.title)}</span>
      <span class="home-law-date">${esc(date.toLocaleDateString("zh-CN"))}</span></a>`;
  }).join("");
}

function parseLocalDate(value) {
  if (!value) return null;
  const date = new Date(String(value).replace(" ", "T"));
  return Number.isFinite(date.getTime()) ? date : null;
}

function renderHomeTrust(stats) {
  const contentDate = stats.content_updated_at || null;
  const success = stats.last_success_sync || null;
  const freshness = contentDate || (success && success.finished_at) || null;
  const date = parseLocalDate(freshness);
  const stale = date && Date.now() - date.getTime() > 7 * 24 * 60 * 60 * 1000;
  const searchable = Number(stats.searchable_articles || 0);
  const embedded = Number(stats.embedded_searchable || 0);
  const coverage = searchable ? Math.round(embedded * 100 / searchable) : 0;
  const mode = stats.mode === "hybrid" ? "混合检索（关键词 + 语义）" : "关键词检索";
  const statuses = Object.keys(stats.by_status || {}).map(key =>
    `${esc(key)} ${stats.by_status[key]}`).join(" · ") || "暂无状态标记";
  $("homeTrust").innerHTML = `
    <div class="trust-primary"><strong>${esc(String(stats.laws || 0))}</strong> 部法规
      <span><strong>${esc(String(searchable))}</strong> 条可检索法条</span></div>
    <dl class="trust-facts">
      <div><dt>内容更新</dt><dd>${esc(contentDate || "未记录")}</dd></div>
      <div><dt>最近成功同步</dt><dd>${esc(success && success.finished_at ? success.finished_at : "未同步")}</dd></div>
      <div><dt>检索模式</dt><dd>${esc(mode)}</dd></div>
      <div><dt>语义覆盖</dt><dd>${embedded} / ${searchable} 条（${coverage}%）</dd></div>
    </dl>
    <div class="trust-status"><span>库内标记状态</span>${statuses}</div>
    <div class="trust-note">语义覆盖仅表示库内法条向量数量，不代表大模型网关健康状态。</div>
    ${!freshness || !date ? '<div class="stale-warning">尚无可用同步日期，数据状态未确认。</div>' : ""}
    ${stale ? '<div class="stale-warning">数据可能过期：库内日期距今已超过 7 天，请核对官方原文。</div>' : ""}`;
}

function renderHomeTopics(stats) {
  const topics = stats.topics || [];
  $("homeTopics").innerHTML = topics.length ? topics.map(topic =>
    `<a class="topic-chip" href="${esc(makeHash("/search", { scope: "law", topic: topic.name }))}">
      ${esc(topic.name)} <span>${esc(String(topic.count))}</span></a>`).join("")
    : '<div class="empty-compact">暂无主题标签，可在法规同步后查看。</div>';
}

async function loadHome(route, ticket) {
  showView("viewHome");
  renderRecentViews();
  const statsRequest = ensureStats().then(stats => ({ stats: stats })).catch(error => ({ error: error }));
  const recentRequest = API.laws({ sort: "updated", page: 1, page_size: LAW_PAGE_SIZE })
    .then(data => ({ data: data })).catch(error => ({ error: error }));
  const results = await Promise.all([statsRequest, recentRequest]);
  if (ticket !== routeSerial) return;
  if (results[0].stats) {
    renderHomeTopics(results[0].stats);
    renderHomeTrust(results[0].stats);
  } else {
    $("homeTopics").innerHTML = '<div class="error-box">主题加载失败，请刷新重试。</div>';
    $("homeTrust").innerHTML = `<div class="error-box">状态加载失败：${esc(String(results[0].error))}</div>`;
  }
  if (results[1].data) {
    const items = results[1].data.items || [];
    $("homeRecentLaws").innerHTML = items.length ? items.map(law => `
      <a class="home-law-row" href="${esc(makeHash("/law/" + law.id))}">
        <span class="home-law-title">${esc(law.title)}</span>
        <span class="home-law-date">${esc(law.updated_at || law.issue_date || "日期未记录")}</span></a>`).join("")
      : '<div class="empty-compact">暂无法规更新记录。</div>';
  } else {
    $("homeRecentLaws").innerHTML = `<div class="error-box">更新记录加载失败：${esc(String(results[1].error))}</div>`;
  }
}

// ---------- 法规列表 ----------
async function loadLawList(route, ticket) {
  showView("viewList");
  const level = route.params.get("level"), status = route.params.get("status");
  const page = positivePage(route.params.get("page"));
  $("listTitle").textContent = level || (status ? `状态：${status}` : "全部法规");
  $("listCount").textContent = "";
  $("lawList").innerHTML = '<div class="loading-box">法规目录加载中…</div>';
  $("pager").innerHTML = "";
  const params = { page: page, page_size: 20 };
  ["level", "status", "q", "topic"].forEach(key => {
    const value = route.params.get(key);
    if (value) params[key] = value;
  });
  let data;
  try {
    data = await API.laws(params);
  } catch (e) {
    if (ticket !== routeSerial) return;
    $("lawList").innerHTML = `<div class="error-box">加载失败：${esc(String(e))}
      <button class="btn-secondary retry-button" type="button">重试</button></div>`;
    $("lawList").querySelector("button").onclick = handleRoute;
    return;
  }
  if (ticket !== routeSerial) return;
  $("listCount").textContent = `共 ${data.total} 部`;
  const box = $("lawList");
  box.innerHTML = "";
  if (!data.items.length) {
    box.innerHTML = '<div class="empty-box">没有符合当前条件的法规，请调整筛选条件。</div>';
  }
  data.items.forEach(law => box.appendChild(lawCard(law)));
  renderPager($("pager"), route, page, data.total, 20, "page");
}

function renderPager(box, route, page, total, pageSize, pageKey) {
  box.innerHTML = "";
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  function pageLink(label, target, enabled) {
    if (!enabled) {
      const span = document.createElement("span");
      span.className = "page-disabled";
      span.textContent = label;
      span.setAttribute("aria-disabled", "true");
      return span;
    }
    const params = paramsObject(route.params);
    params[pageKey] = target;
    const a = document.createElement("a");
    a.className = "page-link";
    a.href = makeHash(route.path, params);
    a.textContent = label;
    return a;
  }
  box.appendChild(pageLink("上一页", page - 1, page > 1));
  const info = document.createElement("span");
  info.className = "page-info";
  info.textContent = `第 ${page} / ${totalPages} 页`;
  box.appendChild(info);
  box.appendChild(pageLink("下一页", page + 1, page < totalPages));
}

function lawCard(law) {
  const card = document.createElement("a");
  card.className = "law-card";
  card.href = makeHash("/law/" + law.id);
  card.innerHTML = `
    <div class="t">${esc(law.title)}</div>
    <div class="meta">
      <span class="badge level">${esc(law.level || "未分类")}</span>
      ${statusBadge(law.status)}
      ${law.issuer ? `<span>${esc(law.issuer)}</span>` : ""}
      ${law.issue_date ? `<span>发布 ${esc(law.issue_date)}</span>` : ""}
      ${law.effective_date ? `<span>施行 ${esc(law.effective_date)}</span>` : ""}
      <span>${esc(law.source_name || "")}</span>
    </div>`;
  return card;
}

function statusBadge(status) {
  const cls = status === "现行有效" ? "ok" : status === "已修订" ? "warn" : "dead";
  return `<span class="badge ${cls}">${esc(status || "未知")}</span>`;
}

// 数据质量提示：unsplit=无条号按全文展示；empty_text=正文异常；duplicate=重复条目
function qualityBadge(law) {
  if (law.quality_flag === "unsplit") {
    return '<span class="badge warn" title="该法规无「第X条」结构，按全文展示">全文展示</span>';
  }
  if (law.quality_flag === "empty_text") {
    return '<span class="badge dead" title="正文过短，数据可能解析异常">数据异常</span>';
  }
  if (law.quality_flag === "duplicate") {
    return '<span class="badge warn" title="与其他法规重复，默认列表中隐藏">重复条目</span>';
  }
  return "";
}

// ---------- 连续法规阅读器 ----------
let articleSearchSerial = 0;

function closeReaderSide(restoreFocus = false) {
  const side = $("readerSide"), wasOpen = side.classList.contains("drawer-open");
  side.classList.remove("drawer-open");
  side.removeAttribute("role");
  side.removeAttribute("aria-modal");
  side.removeAttribute("aria-label");
  $("readerSideBackdrop").classList.add("hidden");
  $("btnReaderSideToggle").setAttribute("aria-expanded", "false");
  if (restoreFocus && wasOpen) {
    if (!$("settingsModal").classList.contains("hidden")) $("cfgApiBase").focus();
    else if (evidenceIsOpen()) $("btnEvidenceClose").focus();
    else if (innerWidth <= 1080) $("btnReaderSideToggle").focus();
  }
}

function openReaderSide() {
  if (innerWidth > 1080 || evidenceIsOpen() ||
      !$("settingsModal").classList.contains("hidden")) return;
  $("readerSide").classList.add("drawer-open");
  $("readerSide").setAttribute("role", "dialog");
  $("readerSide").setAttribute("aria-modal", "true");
  $("readerSide").setAttribute("aria-label", "法规解读与关联");
  $("readerSideBackdrop").classList.remove("hidden");
  $("btnReaderSideToggle").setAttribute("aria-expanded", "true");
  $("btnReaderSideClose").focus();
}

function readerSideFocusable() {
  return Array.prototype.slice.call($("readerSide").querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'))
    .filter(element => !element.closest(".hidden"));
}

function handleReaderSideKeydown(e) {
  const side = $("readerSide");
  if (innerWidth > 1080 || !side.classList.contains("drawer-open") ||
      !$("settingsModal").classList.contains("hidden") ||
      !$("evidenceDrawer").classList.contains("hidden")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopImmediatePropagation();
    closeReaderSide(true);
    return;
  }
  if (e.key !== "Tab") return;
  const items = readerSideFocusable();
  if (!items.length) { e.preventDefault(); return; }
  const first = items[0], last = items[items.length - 1], active = document.activeElement;
  if (e.shiftKey && (active === first || !side.contains(active))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (active === last || !side.contains(active))) {
    e.preventDefault();
    first.focus();
  }
}

function handleReaderSideResize() {
  // 桌面断点没有模态抽屉；清理状态但不聚焦已隐藏的移动端 toggle。
  if (innerWidth > 1080) closeReaderSide(false);
}

function readerRouteIsCurrent(id, ticket, expectedHash) {
  const route = parseRoute();
  return ticket === routeSerial && location.hash === expectedHash &&
    route.name === "law" && route.id === id;
}

function renderReaderWiki(wiki) {
  const box = $("wikiBox"), meta = $("wikiMeta"), button = $("btnWiki");
  if (wiki) {
    box.innerHTML = renderMarkdown(wiki.markdown);
    box.dataset.cached = "1";
    const facts = [];
    if (wiki.generated_at) facts.push("生成于 " + wiki.generated_at);
    if (wiki.model_used) facts.push("模型：" + wiki.model_used);
    meta.textContent = facts.join(" · ");
    button.textContent = "重新生成";
  } else {
    box.innerHTML = '<div class="placeholder">尚未生成解读。点击“生成解读”由大模型按需撰写（会消耗 token）。</div>';
    delete box.dataset.cached;
    meta.textContent = "暂无缓存";
    button.textContent = "生成解读";
  }
  button.disabled = false;
}

function isReaderFulltextArticle(data, a) {
  const index = data.article_index || [];
  return a.id == null || (data.article_total === 1 && index.length === 1 &&
    !String(a.article_no || "").trim());
}

function renderReaderToc(data, route, highlightNo) {
  const box = $("articleToc"), index = data.article_index || [];
  box.innerHTML = "";
  const pageStart = (data.article_page - 1) * data.article_page_size;
  const pageEnd = pageStart + data.article_page_size;
  index.forEach((a, position) => {
    const link = document.createElement("a");
    const fullText = isReaderFulltextArticle(data, a);
    const isTarget = (highlightNo === "全文" && fullText) ||
      (a.id != null && a.id === data.target_article_id);
    link.className = "reader-toc-link" +
      (position >= pageStart && position < pageEnd ? " current-page" : "") +
      (isTarget ? " hit" : "");
    link.href = makeHash(route.path, { article_no: a.article_no || "全文" });
    link.textContent = a.article_no || "全文";
    if (isTarget) link.setAttribute("aria-current", "location");
    box.appendChild(link);
  });
  if (!index.length) box.innerHTML = '<div class="muted">暂无条号目录</div>';
}

function changeReaderPage(page) {
  const route = parseRoute();
  if (route.name !== "law" || !route.id || page < 1) return;
  // 翻页必须移除 article_no，否则后端会再次按旧条号覆盖目标页。
  navigateWith(route, { page: page, article_no: null });
}

function renderReaderPager(data) {
  const totalPages = Math.max(1, Math.ceil(data.article_total / data.article_page_size));
  $("articlePageMeta").textContent = `共 ${data.article_total} 条 · 每页 ${data.article_page_size} 条`;
  $("articlePageInfo").textContent = `第 ${data.article_page} / ${totalPages} 页`;
  $("btnArticlePrev").disabled = data.article_page <= 1;
  $("btnArticleNext").disabled = data.article_page >= totalPages;
  $("btnArticlePrev").onclick = () => changeReaderPage(data.article_page - 1);
  $("btnArticleNext").onclick = () => changeReaderPage(data.article_page + 1);
}

function renderReaderArticles(data, highlightNo) {
  const list = $("articleList");
  list.innerHTML = "";
  let target = null;
  (data.articles || []).forEach(a => {
    const fullText = isReaderFulltextArticle(data, a);
    const isTarget = (highlightNo === "全文" && fullText) ||
      (a.id != null && a.id === data.target_article_id);
    const item = document.createElement("article");
    item.className = "reader-article" + (isTarget ? " hit" : "");
    item.id = fullText ? "art-full" : "art-" + a.id;
    const head = document.createElement("div");
    head.className = "reader-article-head";
    const no = document.createElement("span");
    no.className = "no";
    no.textContent = a.article_no || "全文";
    const copy = document.createElement("button");
    copy.className = "copy-article";
    copy.type = "button";
    copy.textContent = "复制条文";
    copy.setAttribute("aria-label", "复制" + (a.article_no || "全文") + "条文");
    copy.onclick = () => copyArticleText((a.article_no || "全文") + "\n" + (a.text || ""), copy);
    head.appendChild(no);
    head.appendChild(copy);
    const text = document.createElement("div");
    text.className = "reader-article-text";
    text.textContent = a.text || "";
    item.appendChild(head);
    item.appendChild(text);
    list.appendChild(item);
    if (isTarget) target = item;
  });
  if (!data.articles || !data.articles.length) {
    list.innerHTML = '<div class="empty-box">当前页没有可显示的法规正文。</div>';
  }
  return target;
}

async function loadLawDetail(route, ticket = routeSerial) {
  const id = route.id, highlightNo = route.params.get("article_no");
  const locateArticle = Boolean(highlightNo && !route.restoreScroll);
  const expectedHash = location.hash || "#/home";
  showView("viewDetail");
  articleSearchSerial += 1;
  $("articleSearchResults").innerHTML = "";
  $("articleSearchStatus").textContent = "";
  $("btnArticleSearch").disabled = false;
  if (currentLawId !== id) $("articleSearchInput").value = "";
  $("articleList").innerHTML = '<div class="loading-box">法规正文加载中…</div>';
  $("articleToc").innerHTML = '<span class="muted">目录加载中…</span>';
  $("articlePageMeta").textContent = "";
  $("articlePageInfo").textContent = "";
  $("btnArticlePrev").disabled = true;
  $("btnArticleNext").disabled = true;
  if (activeWikiRequests[id]) {
    showWikiGenerating(id);
  } else {
    $("wikiBox").innerHTML = "";
    $("wikiMeta").textContent = "";
    $("btnWiki").disabled = true;
  }
  $("relationList").innerHTML = "";
  $("relationCount").classList.add("hidden");
  $("officialLawLink").classList.add("hidden");
  if (!id) {
    currentLawId = null;
    $("lawHeader").innerHTML = '<h2>无法打开法规</h2><div class="error-box">法规 ID 非法，请从法规目录重新进入。</div>';
    $("articleList").innerHTML = "";
    $("articleToc").innerHTML = "";
    return;
  }
  $("lawHeader").innerHTML = '<div class="loading-box">法规信息加载中…</div>';
  let data;
  try {
    data = await API.lawReader(id, {
      page: positivePage(route.params.get("page")),
      page_size: READER_PAGE_SIZE,
      article_no: highlightNo || null,
    });
  } catch (e) {
    if (!readerRouteIsCurrent(id, ticket, expectedHash)) return;
    $("lawHeader").innerHTML = `<h2>加载失败</h2><div class="error-box">${esc(String(e))}</div>`;
    $("articleList").innerHTML = '<div class="error-box">正文加载失败，请返回后重试。</div>';
    $("articleToc").innerHTML = "";
    return;
  }
  if (!readerRouteIsCurrent(id, ticket, expectedHash)) return;
  const law = data.law;
  currentLawId = id;
  updateChatScope(route);
  recordRecentLaw(id, law.title);
  $("lawHeader").innerHTML = `
    <h2>${esc(law.title)}</h2>
    <div class="meta">
      <span class="badge level">${esc(law.level || "未分类")}</span>
      ${statusBadge(law.status)}
      ${qualityBadge(law)}
      ${law.doc_number ? `<span>${esc(law.doc_number)}</span>` : ""}
      ${law.issuer ? `<span>${esc(law.issuer)}</span>` : ""}
      ${law.issue_date ? `<span>发布 ${esc(law.issue_date)}</span>` : ""}
      ${law.effective_date ? `<span>施行 ${esc(law.effective_date)}</span>` : ""}
    </div>`;
  if (law.source_url && /^https?:\/\//i.test(law.source_url)) {
    $("officialLawLink").href = safeUrl(law.source_url);
    $("officialLawLink").classList.remove("hidden");
  }
  renderReaderToc(data, route, highlightNo);
  const target = renderReaderArticles(data, highlightNo);
  renderReaderPager(data);
  if (activeWikiRequests[id]) showWikiGenerating(id);
  else renderReaderWiki(data.wiki);
  loadRelations(id, ticket, expectedHash);
  if (target) {
    if (locateArticle) {
      setTimeout(() => {
        if (ticket === routeSerial && location.hash === expectedHash) {
          target.scrollIntoView({ block: "center" });
        }
      }, 50);
    }
    setTimeout(() => {
      if (ticket === routeSerial && location.hash === expectedHash) {
        target.classList.remove("hit");
      }
    }, 1800);
  }
}

function articleSnippet(text, q) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  const at = value.toLowerCase().indexOf(q.toLowerCase());
  const start = Math.max(0, at < 0 ? 0 : at - 55);
  const end = Math.min(value.length, start + 190);
  return (start ? "…" : "") + value.slice(start, end) + (end < value.length ? "…" : "");
}

async function searchLawArticles() {
  const route = parseRoute();
  if (route.name !== "law" || !route.id) return;
  const id = route.id, ticket = routeSerial, expectedHash = location.hash || "#/home";
  const requestId = ++articleSearchSerial;
  const q = $("articleSearchInput").value.trim();
  const results = $("articleSearchResults"), status = $("articleSearchStatus");
  if (!q) {
    status.textContent = "请输入篇内搜索关键词。";
    results.innerHTML = "";
    $("btnArticleSearch").disabled = false;
    return;
  }
  status.textContent = "正在搜索本篇法规…";
  results.innerHTML = '<div class="loading-box">篇内搜索中…</div>';
  $("btnArticleSearch").disabled = true;
  try {
    const data = await API.lawArticles(id, { q: q, page: 1, page_size: 100 });
    if (requestId !== articleSearchSerial || !readerRouteIsCurrent(id, ticket, expectedHash)) return;
    const articles = data.articles || [];
    status.textContent = `共 ${data.article_total} 条命中` +
      (data.article_total > articles.length ? `，显示前 ${articles.length} 条` : "");
    results.innerHTML = articles.length ? articles.map(a => `
      <a class="reader-search-hit" href="${esc(makeHash(route.path, { article_no: a.article_no || "全文" }))}">
        <span class="no">${esc(a.article_no || "全文")}</span>
        <span class="snippet">${esc(articleSnippet(a.text, q))}</span>
      </a>`).join("") : '<div class="empty-compact">本篇法规中未找到匹配内容。</div>';
  } catch (e) {
    if (requestId !== articleSearchSerial || !readerRouteIsCurrent(id, ticket, expectedHash)) return;
    status.textContent = "篇内搜索失败。";
    results.innerHTML = `<div class="error-box">${esc(String(e))}
      <button class="btn-secondary retry-button" type="button">重试</button></div>`;
    results.querySelector("button").onclick = searchLawArticles;
  } finally {
    if (requestId === articleSearchSerial && readerRouteIsCurrent(id, ticket, expectedHash)) {
      $("btnArticleSearch").disabled = false;
    }
  }
}

function copyWithTextarea(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "readonly");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  try {
    textarea.focus();
    textarea.select();
    return document.execCommand("copy");
  } catch (_) {
    return false;
  } finally {
    document.body.removeChild(textarea);
  }
}

async function copyArticleText(text, button) {
  let copied = false;
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (_) {
      copied = copyWithTextarea(text);
    }
  } else {
    copied = copyWithTextarea(text);
  }
  button.textContent = copied ? "复制成功" : "复制失败";
  button.classList.toggle("copy-failed", !copied);
  setTimeout(() => {
    if (document.contains(button)) {
      button.textContent = "复制条文";
      button.classList.remove("copy-failed");
    }
  }, 1600);
}

// ---------- 关联法规（双向分组 + 类型筛选） ----------
let relItems = [];        // 当前法规的全部关联项
let relFilter = null;     // 选中的 rel_type（null = 全部）

async function loadRelations(id, ticket = routeSerial, expectedHash = location.hash || "#/home") {
  if (!readerRouteIsCurrent(id, ticket, expectedHash)) return;
  const box = $("relationList");
  box.innerHTML = '<span class="muted">加载中…</span>';
  relFilter = null;
  relItems = [];
  try {
    const data = await API.relations(id);
    if (!readerRouteIsCurrent(id, ticket, expectedHash)) return;
    relItems = data.outgoing.map(r => Object.assign({}, r, { dir: "out" })).concat(
      data.incoming.map(r => Object.assign({}, r, { dir: "in" })));
    $("relationCount").textContent = relItems.length;
    $("relationCount").classList.remove("hidden");
    renderRelations();
  } catch (e) {
    if (!readerRouteIsCurrent(id, ticket, expectedHash)) return;
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
        <a href="${esc(makeHash("/law/" + targetLawId))}">${esc(targetTitle)}</a>
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
}

// ---------- 统一搜索 ----------
const MATCH_REASON_LABELS = {
  exact_article: "精确条号", segment: "分词", keyword: "关键词", vector: "语义",
};

function searchFilters(route) {
  const params = {};
  ["level", "status", "source", "topic"].forEach(key => {
    const value = route.params.get(key);
    if (value) params[key] = value;
  });
  return params;
}

function fillSelect(id, current, entries) {
  const select = $(id), first = select.options[0];
  select.innerHTML = "";
  select.appendChild(first);
  entries.forEach(entry => {
    const option = document.createElement("option");
    option.value = entry[0];
    option.textContent = entry[1];
    select.appendChild(option);
  });
  if (current && !entries.some(entry => entry[0] === current)) {
    const currentOption = document.createElement("option");
    currentOption.value = current;
    currentOption.textContent = current + "（URL 筛选）";
    select.appendChild(currentOption);
  }
  select.value = current || "";
}

function renderSearchFilters(route, stats) {
  const byLevel = stats.by_level || {}, byStatus = stats.by_status || {}, bySource = stats.by_source || {};
  fillSelect("filterLevel", route.params.get("level"), Object.keys(byLevel).map(
    key => [key, `${key}（${byLevel[key]}）`]));
  fillSelect("filterStatus", route.params.get("status"), Object.keys(byStatus).map(
    key => [key, `${key}（${byStatus[key]}）`]));
  fillSelect("filterSource", route.params.get("source"), Object.keys(bySource).map(
    key => [key, `${key}（${bySource[key]}）`]));
  fillSelect("filterTopic", route.params.get("topic"), (stats.topics || []).map(
    topic => [topic.name, `${topic.name}（${topic.count}）`]));
}

function loadSearchFilterOptions(route, ticket) {
  $("filterLoadStatus").textContent = "";
  $("btnRetryFilters").classList.add("hidden");
  ensureStats().then(stats => {
    if (ticket === routeSerial) renderSearchFilters(route, stats);
  }).catch(() => {
    if (ticket !== routeSerial) return;
    renderSearchFilters(route, appStats || {});
    $("filterLoadStatus").textContent = "筛选项加载失败，已保留 URL 当前筛选。";
    $("btnRetryFilters").classList.remove("hidden");
  });
}

function updateSearchTabs(scope) {
  document.querySelectorAll("#searchTabs [data-scope]").forEach(tab => {
    const on = tab.dataset.scope === scope;
    tab.classList.toggle("on", on);
    tab.setAttribute("aria-pressed", on ? "true" : "false");
  });
  $("lawSearchPanel").classList.toggle("hidden", scope === "article");
  $("articleSearchPanel").classList.toggle("hidden", scope === "law");
  $("searchPanelGrid").classList.toggle("single", scope !== "all");
}

function renderSearchError(box, error) {
  box.innerHTML = `<div class="error-box">加载失败：${esc(String(error))}
    <button class="btn-secondary retry-button" type="button">重试</button></div>`;
  box.querySelector("button").onclick = handleRoute;
}

async function loadSearchLaws(route, ticket) {
  const page = positivePage(route.params.get("law_page"));
  const params = searchFilters(route);
  params.page = page;
  params.page_size = LAW_PAGE_SIZE;
  const q = (route.params.get("q") || "").trim();
  if (q) params.q = q;
  $("lawSearchCount").textContent = "";
  $("searchLawResults").innerHTML = '<div class="loading-box">法规检索中…</div>';
  $("searchLawPager").innerHTML = "";
  let data;
  try {
    data = await API.laws(params);
  } catch (e) {
    if (ticket === routeSerial) renderSearchError($("searchLawResults"), e);
    return null;
  }
  if (ticket !== routeSerial) return null;
  $("lawSearchCount").textContent = `共${data.total}部法规`;
  const box = $("searchLawResults");
  box.innerHTML = "";
  if (!data.items.length) {
    box.innerHTML = '<div class="empty-box">未找到匹配法规，请更换关键词或清除部分筛选。</div>';
  } else {
    data.items.forEach(law => box.appendChild(lawCard(law)));
  }
  renderPager($("searchLawPager"), route, page, data.total, LAW_PAGE_SIZE, "law_page");
  return data;
}

function articleCard(result) {
  const params = result.article_no ? { article_no: result.article_no } : {};
  const card = document.createElement("a");
  card.className = "search-hit";
  card.href = makeHash("/law/" + result.law_id, params);
  const reasons = Array.isArray(result.match_reasons) ? result.match_reasons : [];
  const labels = [];
  reasons.forEach(reason => {
    const label = MATCH_REASON_LABELS[reason] || reason;
    if (labels.indexOf(label) < 0) labels.push(label);
  });
  const score = Number(result.score);
  const scoreText = Number.isFinite(score) ? score.toFixed(4) : "—";
  card.innerHTML = `
    <div class="loc"><span>${esc(result.title)}</span><span class="article-loc">${esc(result.article_no || "全文")}</span></div>
    <div class="result-meta">${statusBadge(result.status)}
      ${labels.map(label => `<span class="match-reason">${esc(label)}</span>`).join("")}
      <span class="search-score">排序分 ${esc(scoreText)}</span></div>
    <div class="frag">${highlight(result.snippet || result.text || "", result.highlight_terms || [])}</div>`;
  return card;
}

async function loadSearchArticles(route, ticket) {
  const q = (route.params.get("q") || "").trim();
  if (!q) {
    $("articleSearchCount").textContent = "";
    $("searchArticleResults").innerHTML = '<div class="empty-box">输入关键词后检索法条；法条搜索不支持空查询。</div>';
    $("searchArticlePager").innerHTML = "";
    return { total: 0, count: 0 };
  }
  const page = positivePage(route.params.get("article_page"));
  const params = searchFilters(route);
  params.q = q;
  params.page = page;
  params.page_size = ARTICLE_PAGE_SIZE;
  $("articleSearchCount").textContent = "";
  $("searchArticleResults").innerHTML = '<div class="loading-box">法条检索中…</div>';
  $("searchArticlePager").innerHTML = "";
  let data;
  try {
    data = await API.search(params);
  } catch (e) {
    if (ticket === routeSerial) renderSearchError($("searchArticleResults"), e);
    return null;
  }
  if (ticket !== routeSerial) return null;
  $("articleSearchCount").textContent = `共${data.total}条候选法条 · 本页${data.count}条`;
  const box = $("searchArticleResults");
  box.innerHTML = "";
  if (!data.results.length) {
    box.innerHTML = '<div class="empty-box">未找到候选法条，请尝试更短的关键词或清除部分筛选。</div>';
  } else {
    data.results.forEach(result => box.appendChild(articleCard(result)));
  }
  renderPager($("searchArticlePager"), route, page, data.total, ARTICLE_PAGE_SIZE, "article_page");
  return data;
}

async function loadUnifiedSearch(route, ticket) {
  showView("viewSearch");
  const q = (route.params.get("q") || "").trim();
  const hasFilters = Object.keys(searchFilters(route)).length > 0;
  $("searchPageInput").value = q;
  $("searchTitle").textContent = q ? `搜索：${q}` : "法规与法条";
  $("searchCount").textContent = "";
  updateSearchTabs(route.scope);
  renderSearchFilters(route, appStats || {});
  loadSearchFilterOptions(route, ticket);

  if (!q && !hasFilters && route.scope !== "law") {
    $("lawSearchCount").textContent = "";
    $("articleSearchCount").textContent = "";
    $("searchLawResults").innerHTML = '<div class="empty-box">输入关键词后，将同时检索法规目录与候选法条。</div>';
    $("searchArticleResults").innerHTML = '<div class="empty-box">可尝试“内幕交易”或“证券法 第一百九十一条”。</div>';
    $("searchLawPager").innerHTML = "";
    $("searchArticlePager").innerHTML = "";
    return;
  }

  if (route.scope === "law") {
    const laws = await loadSearchLaws(route, ticket);
    if (ticket === routeSerial && laws) $("searchCount").textContent = `共${laws.total}部法规`;
    return;
  }
  if (route.scope === "article") {
    const articles = await loadSearchArticles(route, ticket);
    if (ticket === routeSerial && articles) $("searchCount").textContent = `共${articles.total}条候选法条`;
    return;
  }
  const lawRequest = loadSearchLaws(route, ticket);
  const articleRequest = loadSearchArticles(route, ticket);
  const results = await Promise.all([lawRequest, articleRequest]);
  if (ticket !== routeSerial) return;
  if (results[0] && results[1]) {
    $("searchCount").textContent = `共${results[0].total}部法规 · 共${results[1].total}条候选法条`;
  } else {
    $("searchCount").textContent = "部分结果加载失败，可在对应区域重试";
  }
}

// 纯文本扫描：最早位置优先，同位置最长词优先，每段独立转义后再拼 mark。
function highlight(text, terms) {
  const source = String(text || "");
  const words = [], seen = new Set();
  (Array.isArray(terms) ? terms : []).forEach(term => {
    const word = String(term || "");
    if (word && !seen.has(word)) { seen.add(word); words.push(word); }
  });
  words.sort((a, b) => b.length - a.length);
  let out = "", plainStart = 0, i = 0;
  while (i < source.length) {
    let hit = "";
    for (const word of words) {
      if (source.substr(i, word.length).toLowerCase() === word.toLowerCase()) {
        hit = word;
        break;
      }
    }
    if (!hit) { i++; continue; }
    out += esc(source.slice(plainStart, i));
    out += `<mark>${esc(source.substr(i, hit.length))}</mark>`;
    i += hit.length;
    plainStart = i;
  }
  return out + esc(source.slice(plainStart));
}

// ---------- 对话（悬浮面板） ----------
function chatPanelOpen() { return !$("chatPanel").classList.contains("hidden"); }

function clampChatPanel(panel) {
  // 面板隐藏时 getBoundingClientRect 为 0，不能夹取，否则会破坏默认尺寸
  if (!panel || panel.classList.contains("hidden")) return;
  const r = panel.getBoundingClientRect();
  const maxW = Math.max(320, innerWidth - 16);
  const maxH = Math.max(360, innerHeight - 16);
  const w = Math.min(Math.max(320, r.width), maxW);
  const h = Math.min(Math.max(360, r.height), maxH);
  let x = r.left, y = r.top;
  if (panel.style.right !== "auto" || panel.style.bottom !== "auto") {
    x = innerWidth - 24 - w;
    y = innerHeight - 92 - h;
  }
  x = Math.min(Math.max(8, x), Math.max(8, innerWidth - w - 8));
  y = Math.min(Math.max(8, y), Math.max(8, innerHeight - h - 8));
  panel.style.width = w + "px";
  panel.style.height = h + "px";
  panel.style.left = x + "px";
  panel.style.top = y + "px";
  panel.style.right = "auto";
  panel.style.bottom = "auto";
}

function toggleChat(open) {
  const panel = $("chatPanel");
  panel.classList.toggle("hidden", !open);
  if (open) {
    $("chatDot").classList.add("hidden");  // 打开即消红点
    clampChatPanel(panel);                 // 窗口变小后再次打开时收进视口
    const log = $("chatLog");
    log.scrollTop = log.scrollHeight;
    $("chatInput").focus();
  }
}

function initChatPanel() {
  $("chatFab").onclick = () => toggleChat(!chatPanelOpen());
  $("btnMinChat").onclick = () => toggleChat(false);

  const head = $("chatPanelHead"), panel = $("chatPanel");
  const resizeHandle = $("chatResize");
  let drag = null, resize = null;

  // 标题栏拖动：记录位置到 localStorage（夹在视口内，防拖丢）
  head.addEventListener("mousedown", e => {
    if (e.target.closest("button")) return;  // 最小化按钮不触发拖动
    const r = panel.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    e.preventDefault();
  });

  // 右下角手柄：自由调节宽高；先转成 left/top 定位，让右下角跟随鼠标
  resizeHandle.addEventListener("mousedown", e => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const r = panel.getBoundingClientRect();
    panel.style.left = r.left + "px";
    panel.style.top = r.top + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    resize = {
      sx: e.clientX, sy: e.clientY, w: r.width, h: r.height,
      right: r.left + r.width, bottom: r.top + r.height
    };
    document.body.classList.add("chat-resizing");
  });

  resizeHandle.addEventListener("dblclick", () => {
    panel.style.width = "400px";
    panel.style.height = "550px";
    safeStorageRemove("chatPanelSize");
    clampChatPanel(panel);
  });

  window.addEventListener("mousemove", e => {
    if (resize) {
      const dx = e.clientX - resize.sx, dy = e.clientY - resize.sy;
      const w = Math.min(innerWidth - 16, Math.max(320, resize.w + dx));
      const h = Math.min(innerHeight - 16, Math.max(360, resize.h + dy));
      // 右下角跟随鼠标；贴到视口边缘时面板向左/上扩展，避免宽度被卡死
      const right = resize.right + dx, bottom = resize.bottom + dy;
      const x = Math.min(Math.max(8, right - w), Math.max(8, innerWidth - w - 8));
      const y = Math.min(Math.max(8, bottom - h), Math.max(8, innerHeight - h - 8));
      panel.style.width = w + "px";
      panel.style.height = h + "px";
      panel.style.left = x + "px";
      panel.style.top = y + "px";
      return;
    }
    if (!drag) return;
    const w = panel.offsetWidth, h = panel.offsetHeight;
    const x = Math.min(Math.max(e.clientX - drag.dx, 0), innerWidth - w);
    const y = Math.min(Math.max(e.clientY - drag.dy, 0), innerHeight - h);
    panel.style.left = x + "px";
    panel.style.top = y + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    safeStorageSet("chatPanelPos", JSON.stringify({ x, y }));
  });

  window.addEventListener("mouseup", () => {
    drag = null;
    if (resize) {
      resize = null;
      document.body.classList.remove("chat-resizing");
      const r = panel.getBoundingClientRect();
      safeStorageSet("chatPanelSize", JSON.stringify({
        w: Math.round(r.width), h: Math.round(r.height)
      }));
      safeStorageSet("chatPanelPos", JSON.stringify({
        x: Math.round(r.left), y: Math.round(r.top)
      }));
    }
  });

  window.addEventListener("resize", () => { clampChatPanel(panel); });

  // 恢复上次尺寸与拖动位置（先尺寸后位置，位置按新尺寸夹取）
  try {
    const size = JSON.parse(localStorage.getItem("chatPanelSize") || "null");
    if (size && size.w >= 320 && size.h >= 360) {
      panel.style.width = size.w + "px";
      panel.style.height = size.h + "px";
    }
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
  } catch (_) { /* 忽略损坏的位置/尺寸记录 */ }
}

let activeChatRequest = null;
let chatRequestSerial = 0;
let chatRequests = [];
let chatScopeKey = "all";

function resetChatForScope(nextKey) {
  if (nextKey === chatScopeKey) return;
  chatScopeKey = nextKey;
  if (activeChatRequest) {
    const request = activeChatRequest;
    stopChat();
    request.invalidated = true;
    request.superseded = true;
  }
  chatRequests.forEach(request => {
    request.superseded = true;
    updateChatRetryButton(request);
  });
  chatRequestSerial++;
  chatRequests = [];
  chatHistory = [];
  $("chatDot").classList.add("hidden");
  $("chatLog").innerHTML = '<div class="chat-hint">已切换研究范围，开始新对话。</div>';
}

function updateChatScope(route) {
  if (route && (route.name !== "law" || route.id == null || currentLawId !== route.id)) {
    currentLawId = null;
  }
  const valid = Number.isInteger(currentLawId) && currentLawId > 0;
  const select = $("chatScope");
  const currentOption = select.querySelector('option[value="current"]');
  currentOption.disabled = !valid;
  if (!valid && select.value === "current") select.value = "all";
  $("btnAskLaw").disabled = !valid;
  select.title = valid ? "可仅依据当前打开的法规研究，或检索全库" : "打开法规详情后可选择当前法规";
  const nextKey = valid && select.value === "current" ? "law:" + currentLawId : "all";
  resetChatForScope(nextKey);
}

function setChatBusy(busy) {
  $("btnSend").disabled = busy;
  $("btnStopChat").disabled = !busy;
  $("btnStopChat").textContent = "停止";
  $("chatScope").disabled = busy;
}

function supportsChatStream() {
  return typeof ReadableStream !== "undefined" && !!ReadableStream.prototype &&
    typeof ReadableStream.prototype.getReader === "function" &&
    typeof TextDecoder === "function" && typeof AbortController === "function";
}

async function readChatStream(response, onStage) {
  const reader = response.body && typeof response.body.getReader === "function"
    ? response.body.getReader() : null;
  const decoder = reader ? new TextDecoder("utf-8") : null;
  let buffer = "", result = null;

  function handleLine(line) {
    if (!line.trim()) return;
    let event;
    try { event = JSON.parse(line); }
    catch (_) { throw new Error("对话进度数据格式错误"); }
    if (!event || typeof event.type !== "string") {
      throw new Error("对话进度数据缺少事件类型");
    }
    if (event.type === "stage") {
      if (onStage) onStage(event);
      return;
    }
    if (event.type === "result") {
      result = event.data;
      return;
    }
    if (event.type === "cancelled") {
      const error = new Error("本次研究已停止");
      error.chatCancelled = true;
      error.chatTerminal = true;
      throw error;
    }
    if (event.type === "error") {
      const error = new Error(event.error || "对话研究失败");
      error.chatTerminal = true;
      throw error;
    }
  }

  try {
    if (reader) {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop();
        for (const line of lines) handleLine(line);
      }
      buffer += decoder.decode();
    } else {
      // 流请求已经发出，意外无 reader 时只读取当前响应；禁止再次 POST 旧接口。
      buffer = await response.text();
    }
    const lines = buffer.split(/\r?\n/);
    for (const line of lines) handleLine(line);
    if (!result) throw new Error("对话响应未包含最终结果");
    return result;
  } catch (error) {
    if (reader && !error.chatTerminal) {
      // 非终局解析、stage 回调和读取异常必须协作取消；次生取消错误不覆盖原错。
      try { await reader.cancel(); } catch (_) { /* 保留原异常 */ }
    }
    throw error;
  }
}

async function requestChat(messages, lawId, signal, onStage) {
  // 旧浏览器没有流读取能力时只请求原 JSON 接口，不把完整回答伪装成 token 流。
  if (!supportsChatStream()) return API.chat(messages, lawId, signal);
  const response = await API.chatStream(messages, lawId, signal);
  if (!response.ok) return j(response);
  return readChatStream(response, onStage);
}

function chatStageText(event) {
  const labels = {
    thinking: "正在思考",
    model_done: "模型已返回，正在检查后续步骤",
    searching: "正在检索",
    search_done: "检索完成",
    citing: "正在整理引用",
  };
  let text = labels[event.stage] || "正在研究";
  if (event.round) text += "（第 " + event.round + " 轮）";
  if (event.stage === "searching" && event.query) text += "：" + event.query;
  return text + "…";
}

function renderChatStage(aiDiv, text, stage) {
  const stageAttr = stage ? ` data-stage="${esc(stage)}"` : "";
  aiDiv.innerHTML = `<div class="chat-progress"${stageAttr} aria-live="polite">${esc(text)}</div>`;
  const log = $("chatLog");
  log.scrollTop = log.scrollHeight;
}

function renderChatResult(aiDiv, r) {
  let html = "";
  if (r.searches && r.searches.length) {
    html += `<div class="searches">已检索：${r.searches.map(esc).join("；")}</div>`;
  }
  html += '<div class="chat-answer">' + renderMarkdown(r.content) + "</div>";
  const citations = r.citations || [];
  if (citations.length) {
    html += '<div class="cite-row" aria-label="本轮回答引用">' + citations.map((c, index) => {
      const url = safeUrl(c.source_url);
      const src = url !== "#"
        ? `<a class="cite-src" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="打开官方原文" aria-label="打开官方原文">↗</a>`
        : "";
      const label = `📖 ${esc(c.title)} ${esc(c.article_no || "条号未定位")}`;
      if (c.law_id == null) {
        // 未收录引用仍完整保留；没有真实 law_id 时不提供库内跳转。
        return `<span class="cite-wrap"><span class="cite-chip cite-unmatched" title="库内未收录该法规原文">${label}</span>${src}</span>`;
      }
      const hint = c.article_id != null ? "点击核对法条原文" : "点击查看法规信息（条号未定位）";
      return `<span class="cite-wrap"><button type="button" class="cite-chip cite-reference" data-citation="${index}" title="${hint}"><span class="cite-main">${label}</span></button>${src}</span>`;
    }).join("") + "</div>";
  }
  aiDiv.innerHTML = html;
  bindCitationActions(aiDiv, r);
}

function bindInlineCitation(chip, handler, title) {
  chip.classList.add("cite-inline");
  chip.title = title;
  chip.tabIndex = 0;
  chip.setAttribute("role", "button");
  chip.onclick = handler;
  chip.onkeydown = e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    handler();
  };
}

function bindCitationActions(aiDiv, r) {
  const citations = r.citations || [];
  const citeRow = aiDiv.querySelector(".cite-row");
  // 正文引用只与本轮 citations 匹配，无法可靠匹配时定位并高亮引用列表。
  aiDiv.querySelectorAll(".chat-answer .cite-chip").forEach(chip => {
    const hit = findCitation(citations, parseCiteText(chip.textContent));
    if (hit && hit.law_id != null) {
      bindInlineCitation(chip, () => openEvidence(hit), hit.article_id != null
        ? "点击核对法条原文" : "点击查看法规信息（条号未定位）");
    } else if (citeRow) {
      bindInlineCitation(chip, () => {
        citeRow.scrollIntoView({ block: "nearest" });
        citeRow.classList.add("cite-flash");
        setTimeout(() => citeRow.classList.remove("cite-flash"), 1200);
      }, "点击查看下方引用列表");
    }
  });
  aiDiv.querySelectorAll(".cite-chip[data-citation]").forEach(chip => {
    const index = parseInt(chip.getAttribute("data-citation"), 10);
    const citation = citations[index];
    if (!citation || citation.law_id == null) return;
    chip.onclick = () => openEvidence(citation);
  });
  // 官方链接是引用按钮的兄弟节点；阻止冒泡，避免一次点击打开两个界面。
  aiDiv.querySelectorAll(".cite-src").forEach(a => {
    a.addEventListener("click", e => e.stopPropagation());
  });
}

function pruneChatRequests() {
  chatRequests = chatRequests.filter(request => {
    if (activeChatRequest === request) return true;
    // 非 active 只保留当前可重试失败项；成功、 superseded 或旧 serial 均删除。
    return request.completed && !request.succeeded && !request.superseded &&
      request.retryButton && request.serial === chatRequestSerial;
  });
}

function updateChatRetryButton(request) {
  const button = request.retryButton;
  if (!button) return;
  const stale = request.superseded || request.serial !== chatRequestSerial;
  const waitingOwn = activeChatRequest === request;
  const waiting = !!activeChatRequest;
  button.disabled = stale || waiting;
  button.dataset.contextSuperseded = stale ? "true" : "false";
  button.textContent = stale ? "上下文已更新" : (waitingOwn ? "等待原请求结束" :
    (waiting ? "等待当前请求结束" : "重试本次问题"));
  button.title = stale ? "已发送更新问题，旧问题不可重试" :
    (waiting ? "原请求尚未结束，暂不可重试" : "使用原问题重新研究");
}

function supersedeOtherChatRequests(current) {
  chatRequests.forEach(request => {
    if (request === current) return;
    request.superseded = true;
    updateChatRetryButton(request);
  });
  pruneChatRequests();
}

function renderChatFailure(aiDiv, request, error, cancelled) {
  const message = cancelled ? "本次研究已停止。" : "研究失败：" + String(error && error.message ? error.message : error);
  aiDiv.innerHTML = `<div class="chat-error" role="alert">${esc(message)}</div>
    <div class="chat-cancel-note">当前已发起的模型请求可能仍需结束后才能停止。</div>
    <button class="btn-secondary chat-retry" type="button">重试本次问题</button>`;
  request.retryButton = aiDiv.querySelector(".chat-retry");
  request.retryButton.onclick = () => retryChat(request);
  updateChatRetryButton(request);
}

function chatRequestIsCurrent(request) {
  return activeChatRequest === request && !request.invalidated && !request.superseded &&
    request.serial === chatRequestSerial;
}

async function performChatRequest(request) {
  activeChatRequest = request;
  request.completed = false;
  request.succeeded = false;
  request.retryButton = null;
  setChatBusy(true);
  renderChatStage(request.aiDiv, "正在发起研究请求…", "requesting");
  try {
    const r = await requestChat(request.messages, request.lawId,
      request.controller ? request.controller.signal : null, event => {
        if (chatRequestIsCurrent(request)) {
          renderChatStage(request.aiDiv, chatStageText(event), event.stage);
        }
      });
    if (!chatRequestIsCurrent(request)) return;
    if (!r || typeof r.content !== "string") throw new Error("对话最终结果格式不完整");
    renderChatResult(request.aiDiv, r);
    // 只有完整成功结果才进入下一轮上下文；失败和停止均不写 assistant。
    chatHistory.push({ role: "assistant", content: r.content });
    trimChatHistory();
    request.succeeded = true;
  } catch (e) {
    if (!chatRequestIsCurrent(request)) return;
    if (request.controller) {
      try { request.controller.abort(); } catch (_) { /* 保留对话原错误 */ }
    }
    const cancelled = request.stopped || e.chatCancelled || e.name === "AbortError";
    renderChatFailure(request.aiDiv, request, e, cancelled);
  } finally {
    request.completed = true;
    if (activeChatRequest === request) {
      activeChatRequest = null;
      setChatBusy(false);
      updateChatRetryButton(request);
      if (!chatPanelOpen()) $("chatDot").classList.remove("hidden");
    }
    pruneChatRequests();
  }
}

function stopChat() {
  const request = activeChatRequest;
  if (!request || request.stopped) return;
  request.stopped = true;
  $("btnStopChat").disabled = true;
  $("btnStopChat").textContent = "停止中…";
  renderChatStage(request.aiDiv,
    "已请求停止；当前已发起的模型请求可能仍需结束后才能停止。", "cancelling");
  if (request.controller) {
    // 中止 fetch 等待；服务端在连接断开后设置协作取消标记，后续阶段停止。
    request.controller.abort();
  } else {
    // 极旧浏览器无 AbortController：忽略旧响应，但保持 active 到原请求 finally，
    // 期间 retry 禁用，避免原 HTTP 尚未结束时重复 POST。
    request.invalidated = true;
    renderChatFailure(request.aiDiv, request, new Error("已停止本地等待"), true);
  }
}

function canRetryChatRequest(request) {
  return !!request && !activeChatRequest && !request.superseded &&
    request.serial === chatRequestSerial;
}

async function retryChat(request) {
  if (!canRetryChatRequest(request)) {
    if (request) updateChatRetryButton(request);
    return;
  }
  supersedeOtherChatRequests(request);
  // 重试也是新一代上下文；旧 serial 的其他 retry 全部失效。
  request.serial = ++chatRequestSerial;
  request.superseded = false;
  request.invalidated = false;
  request.controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  request.stopped = false;
  await performChatRequest(request);
}

function trimChatHistory() {
  // 保留最近且完整的问答边界：不超过 80 条，首条不能是孤儿 assistant。
  let start = Math.max(0, chatHistory.length - 80);
  while (start < chatHistory.length && chatHistory[start].role === "assistant") start++;
  chatHistory = chatHistory.slice(start);
}

async function sendChat() {
  if (activeChatRequest) return;
  const input = $("chatInput");
  const q = input.value.trim();
  if (!q) return;
  supersedeOtherChatRequests(null);
  updateChatScope(parseRoute());
  const lawId = $("chatScope").value === "current" && currentLawId != null ? currentLawId : null;
  input.value = "";
  appendMsg("user", q);
  chatHistory.push({ role: "user", content: q });
  trimChatHistory();
  const request = {
    serial: ++chatRequestSerial,
    messages: chatHistory.slice(),
    lawId: lawId,
    controller: typeof AbortController !== "undefined" ? new AbortController() : null,
    aiDiv: appendMsg("ai", ""),
    stopped: false,
    invalidated: false,
    superseded: false,
    completed: false,
    succeeded: false,
    retryButton: null,
  };
  chatRequests.push(request);
  await performChatRequest(request);
}

function clearChat() {
  const hasContent = chatHistory.length > 0 || !!$("chatLog").querySelector(".msg") ||
    !!$("chatInput").value.trim();
  if (!hasContent) return;
  if (!confirm("确定清空当前对话？此操作不可撤销。")) return;
  if (activeChatRequest) {
    const request = activeChatRequest;
    stopChat();
    request.invalidated = true;
    request.superseded = true;
  }
  // 清空也是新上下文；active 请求由自身 finally 恢复按钮，旧 serial 不可重试。
  chatRequestSerial++;
  chatRequests = [];
  chatHistory = [];
  $("chatInput").value = "";
  $("chatDot").classList.add("hidden");
  $("chatLog").innerHTML = '<div class="chat-hint">对话已清空。</div>';
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

// ---------- 引用证据抽屉 ----------
let evidenceReturnFocus = null;
let evidenceSerial = 0;

function evidenceIsOpen() {
  return !$("evidenceDrawer").classList.contains("hidden");
}

function closeEvidence(restoreFocus = true) {
  const wasOpen = evidenceIsOpen();
  evidenceSerial++;
  $("evidenceDrawer").classList.add("hidden");
  $("evidenceBackdrop").classList.add("hidden");
  const target = evidenceReturnFocus;
  evidenceReturnFocus = null;
  if (restoreFocus && wasOpen) {
    // 仍有高层/覆盖层时，焦点不能落回被遮挡的聊天或阅读器。
    if (!$("settingsModal").classList.contains("hidden")) {
      $("cfgApiBase").focus();
      return;
    }
    if ($("readerSide").classList.contains("drawer-open")) {
      $("btnReaderSideClose").focus();
      return;
    }
    if (target && document.contains(target)) {
      try { target.focus({ preventScroll: true }); }
      catch (_) { target.focus(); }
    }
  }
}

function evidenceFocusable() {
  return Array.prototype.slice.call($("evidenceDrawer").querySelectorAll(
    'a[href]:not(.hidden), button:not([disabled]), input:not([disabled]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'));
}

function handleEvidenceKeydown(e) {
  const drawer = $("evidenceDrawer");
  if (drawer.classList.contains("hidden") || !$("settingsModal").classList.contains("hidden")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopImmediatePropagation();
    closeEvidence(true);
    return;
  }
  if (e.key !== "Tab") return;
  const items = evidenceFocusable();
  if (!items.length) {
    e.preventDefault();
    drawer.focus();
    return;
  }
  const first = items[0], last = items[items.length - 1], active = document.activeElement;
  if (e.shiftKey && (active === first || !drawer.contains(active))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (active === last || !drawer.contains(active))) {
    e.preventDefault();
    first.focus();
  }
}

function renderEvidenceLaw(law, sourceUrl) {
  const source = sourceUrl !== "#"
    ? `<a class="evidence-source" href="${esc(sourceUrl)}" target="_blank" rel="noopener noreferrer">官方原文 ↗</a>`
    : "";
  return `<section class="evidence-law">
    <div class="evidence-law-heading"><h3>${esc(law.title)}</h3>${source}</div>
    <div class="evidence-meta">
      <span>库内时效状态：${esc(law.status || "未标记")}</span>
      ${law.doc_number ? `<span>发文字号：${esc(law.doc_number)}</span>` : ""}
      ${law.issuer ? `<span>发布机关：${esc(law.issuer)}</span>` : ""}
      ${law.effective_date ? `<span>施行日期：${esc(law.effective_date)}</span>` : ""}
    </div>
  </section>`;
}

async function openEvidence(c) {
  if (!c || c.law_id == null) return;
  const ticket = ++evidenceSerial;
  if (!evidenceIsOpen()) evidenceReturnFocus = document.activeElement;
  closeReaderSide();
  const readerHash = makeHash("/law/" + c.law_id,
    c.article_id != null ? { article_no: c.article_no } : {});
  $("evidenceOpenReader").href = readerHash;
  $("evidenceOpenReader").classList.add("hidden");
  $("evidenceTitle").textContent = "证据原文";
  $("evidenceBody").innerHTML = '<div class="loading-box">证据加载中…</div>';
  $("evidenceBackdrop").classList.remove("hidden");
  $("evidenceDrawer").classList.remove("hidden");
  $("btnEvidenceClose").focus();
  try {
    const data = await API.lawReader(c.law_id, {
      page: 1,
      page_size: READER_PAGE_SIZE,
      article_no: c.article_id != null ? c.article_no : null,
    });
    if (ticket !== evidenceSerial) return;
    if (!data || !data.law) {
      $("evidenceBody").innerHTML = '<div class="empty-box">未找到可展示的法规证据。</div>';
      return;
    }
    const sourceUrl = safeUrl(data.law.source_url || c.source_url);
    let html = renderEvidenceLaw(data.law, sourceUrl);
    if (c.article_id != null) {
      const articles = data.articles || [];
      // 必须命中真实 article_id，不能用当前分页第一条冒充目标条文。
      const article = articles.filter(a => a.id === c.article_id)[0];
      if (!article) {
        html += '<div class="empty-box">条号已请求定位，但当前响应中未找到对应条文。可进入阅读器继续核对。</div>';
      } else {
        html += `<article class="evidence-article">
          <h3>${esc(article.article_no || c.article_no || "目标条文")}</h3>
          ${article.text ? `<div class="evidence-article-text">${esc(article.text)}</div>`
            : '<div class="empty-compact">目标条文内容为空。</div>'}
        </article>`;
      }
    } else {
      // 仅法规命中时没有目标条号，展示元数据而不展示分页首条。
      html += '<div class="empty-compact evidence-notice">条号未定位，仅展示法规元数据。请进入阅读器查找并核对具体条文。</div>';
    }
    $("evidenceBody").innerHTML = html;
    $("evidenceOpenReader").classList.remove("hidden");
  } catch (e) {
    if (ticket !== evidenceSerial) return;
    $("evidenceBody").innerHTML = `<div class="error-box">证据加载失败：${esc(String(e))}</div>`;
    $("evidenceOpenReader").classList.remove("hidden");
  }
}

// ---------- 解读 / 关联 tabs 与 wiki ----------
const activeWikiRequests = {};

function showWikiGenerating(id) {
  const route = parseRoute();
  if (route.name !== "law" || route.id !== id) return;
  $("wikiBox").innerHTML = '<div class="placeholder">生成中…（大模型撰写解读，约需 30-60 秒；可继续阅读或翻页）</div>';
  $("wikiMeta").textContent = "正在生成解读";
  $("btnWiki").textContent = "生成中…";
  $("btnWiki").disabled = true;
}

function activateReaderTab(tabId, moveFocus = false) {
  const tabs = Array.prototype.slice.call(document.querySelectorAll('#readerTabs [role="tab"]'));
  tabs.forEach(tab => {
    const selected = tab.id === tabId;
    tab.setAttribute("aria-selected", selected ? "true" : "false");
    tab.setAttribute("tabindex", selected ? "0" : "-1");
    $(tab.getAttribute("aria-controls")).classList.toggle("hidden", !selected);
    if (selected && moveFocus) tab.focus();
  });
}

function handleReaderTabKeydown(e) {
  const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
  if (keys.indexOf(e.key) < 0) return;
  const tabs = Array.prototype.slice.call(document.querySelectorAll('#readerTabs [role="tab"]'));
  const current = tabs.indexOf(e.currentTarget);
  if (current < 0) return;
  let next = current;
  if (e.key === "Home") next = 0;
  else if (e.key === "End") next = tabs.length - 1;
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (current + tabs.length - 1) % tabs.length;
  else next = (current + 1) % tabs.length;
  e.preventDefault();
  activateReaderTab(tabs[next].id, true);
}

async function generateWiki(force) {
  const initialRoute = parseRoute();
  if (initialRoute.name !== "law" || !initialRoute.id) return;
  const id = initialRoute.id;
  const box = $("wikiBox"), button = $("btnWiki");
  if (button.disabled || activeWikiRequests[id]) return;
  const regenerate = Boolean(force && box.dataset.cached === "1");
  if (regenerate && !confirm("重新生成解读会消耗 token，是否继续？")) return;
  const request = { lawId: id };
  activeWikiRequests[id] = request;
  showWikiGenerating(id);
  try {
    const r = await API.wiki(id, regenerate);
    const route = parseRoute();
    if (route.name !== "law" || route.id !== id) return;
    if (r.error) {
      box.innerHTML = `<div class="error-box">生成失败：${esc(r.error)}</div>`;
      return;
    }
    renderReaderWiki(r);
  } catch (e) {
    const route = parseRoute();
    if (route.name !== "law" || route.id !== id) return;
    box.innerHTML = `<div class="error-box">生成失败：${esc(String(e))}</div>`;
  } finally {
    if (activeWikiRequests[id] === request) delete activeWikiRequests[id];
    const route = parseRoute();
    if (route.name === "law" && route.id === id) {
      button.disabled = false;
      button.textContent = box.dataset.cached === "1" ? "重新生成" : "生成解读";
    }
  }
}

// ---------- 配置弹窗 ----------
let settingsReturnFocus = null;
let settingsSerial = 0;
let settingsOpening = false;

async function openSettings() {
  if (settingsOpening || !$("settingsModal").classList.contains("hidden")) return;
  const ticket = ++settingsSerial;
  settingsOpening = true;
  settingsReturnFocus = document.activeElement;
  $("btnSettings").disabled = true;
  $("cfgStatus").textContent = "读取配置中…";
  $("cfgStatus").className = "cfg-status";
  $("settingsModal").classList.remove("hidden");
  $("cfgApiBase").focus();
  let s;
  try {
    s = await API.getSettings();
  } catch (e) {
    if (ticket === settingsSerial && !$("settingsModal").classList.contains("hidden")) {
      alert(`读取配置失败：${String(e)}`);
    }
    return;
  } finally {
    if (ticket === settingsSerial) {
      settingsOpening = false;
      $("btnSettings").disabled = false;
    }
  }
  if (ticket !== settingsSerial || $("settingsModal").classList.contains("hidden")) return;
  $("cfgApiBase").value = s.api_base || "";
  $("cfgApiKey").value = "";
  $("cfgApiKey").placeholder = s.has_key ? `当前：${s.api_key_masked}（留空保留）` : "必填";
  $("cfgChatModel").value = s.chat_model || "";
  $("cfgEmbedModel").value = s.embedding_model || "";
  $("cfgTemp").value = s.temperature;
  $("cfgStatus").textContent = "";
  $("cfgStatus").className = "cfg-status";
}

function closeSettings() {
  settingsSerial++;
  settingsOpening = false;
  $("btnSettings").disabled = false;
  $("settingsModal").classList.add("hidden");
  const target = settingsReturnFocus;
  settingsReturnFocus = null;
  // settings 关闭后，焦点只能进入当前仍可见的最上层，不能回到被遮挡层。
  if (evidenceIsOpen()) {
    $("btnEvidenceClose").focus();
    return;
  }
  if ($("readerSide").classList.contains("drawer-open")) {
    $("btnReaderSideClose").focus();
    return;
  }
  if (target && document.contains(target)) {
    try { target.focus({ preventScroll: true }); }
    catch (_) { target.focus(); }
  }
}

function settingsFocusable() {
  return Array.prototype.slice.call($("settingsModal").querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'));
}

function handleSettingsKeydown(e) {
  const modal = $("settingsModal");
  if (modal.classList.contains("hidden")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopImmediatePropagation();
    closeSettings();
    return;
  }
  if (e.key === "Tab") {
    const items = settingsFocusable();
    if (!items.length) { e.preventDefault(); return; }
    const first = items[0], last = items[items.length - 1], active = document.activeElement;
    if (e.shiftKey && (active === first || !modal.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !modal.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  }
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
  // 保持 ES2017 语法底线，避免旧内网浏览器整份脚本解析失败。
  const lines = String(md == null ? "" : md).split("\n");
  let html = "", inList = null;
  const flush = () => { if (inList) { html += `</${inList}>`; inList = null; } };
  for (let raw of lines) {
    const line = raw.replace(/\s+$/, "");   // 正则去除行尾空白，兼容旧浏览器
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

// 正文引用匹配：解析【法规名·第X条】，在同轮 citations 中找对应法规/条号
function cnToInt(s) {
  const digits = "零一二三四五六七八九";
  s = String(s || "").replace(/〇/g, "零");
  if (/^\d+$/.test(s)) return parseInt(s, 10);
  let section = 0, num = 0;
  for (let i = 0; i < s.length; i++) {
    const ch = s.charAt(i);
    const d = digits.indexOf(ch);
    if (d >= 0) num = d;
    else if (ch === "十") { section += (num || 1) * 10; num = 0; }
    else if (ch === "百") { section += (num || 1) * 100; num = 0; }
    else if (ch === "千") { section += (num || 1) * 1000; num = 0; }
  }
  return section + num;
}

function outerBookTitleEnd(title) {
  if (title.charAt(0) !== "《") return -1;
  let depth = 0;
  for (let i = 0; i < title.length; i++) {
    if (title.charAt(i) === "《") depth++;
    else if (title.charAt(i) === "》") {
      depth--;
      if (depth === 0) return i;
      if (depth < 0) return -1;
    }
  }
  return -1;
}

function cleanCitationTitle(s) {
  let title = String(s || "").trim();
  title = title.replace(/^【[^】]*】/, "");
  // 仅去发文字号，不去普通括号、司法解释前后缀或标题组成部分。
  title = title.replace(/[（(](?:证监会令|证监会公告|主席令|国务院令|财政部令|法释|银发|发改)[^）)]*[）)]/g, "").trim();
  const outer = title.match(/^《([^》]+)》$/);
  const outerEnd = outerBookTitleEnd(title);
  if (outer) {
    title = outer[1];
  } else if (outerEnd === title.length - 1) {
    // 司法解释内部还可能有书名号，按嵌套深度只去最外层。
    title = title.slice(1, -1);
  } else if (outerEnd > 0) {
    // 外层书名号后仅跟明确年份版本时，去年代书名号但保留版本。
    const suffix = title.slice(outerEnd + 1);
    if (/^[（(]\d{4}年?(?:修订|修正)?[）)]$/.test(suffix)) {
      title = title.slice(1, outerEnd) + suffix;
    }
  }
  // 位于标题中间的书名号是标题语义的一部分，不能提取内部名称。
  return title.trim();
}

function citationTitleKey(s) {
  // 唯一前后缀等价规则：完整标题可去/留“中华人民共和国”，禁止任意子串匹配。
  return cleanCitationTitle(s).replace(/^中华人民共和国/, "");
}

function citationTitleHasYear(s) {
  return /[（(]\d{4}年?(?:修订|修正)?[）)]/.test(String(s || ""));
}

function citationTitleVariants(s, includeWithoutYear) {
  const cleaned = cleanCitationTitle(s);
  if (!cleaned) return [];
  const withoutVersion = cleaned.replace(/[（(]\d{4}年?(?:修订|修正)?[）)]/g, "").trim();
  const bases = includeWithoutYear && withoutVersion && withoutVersion !== cleaned
    ? [cleaned, withoutVersion] : [cleaned];
  const aliases = [
    ["最高检", "最高人民检察院"],
    ["最高法", "最高人民法院"],
    ["证监会", "中国证券监督管理委员会"],
  ];
  const out = [];
  bases.forEach(base => {
    const titles = [base];
    aliases.forEach(alias => {
      // 与后端 _title_variants 一致，仅展开明确的机关别名。
      if (base.indexOf(alias[0]) >= 0) titles.push(base.split(alias[0]).join(alias[1]));
    });
    titles.forEach(title => {
      const key = citationTitleKey(title);
      if (key && out.indexOf(key) < 0) out.push(key);
    });
  });
  return out;
}

function citationTitleMatches(c, title, includeWithoutYear) {
  const requested = citationTitleVariants(title, includeWithoutYear);
  if (!requested.length) return false;
  // 后端可能以原始别名命中、在 c.title 返回库内正式名，两个字段都必须比较。
  const candidates = citationTitleVariants(c.title, includeWithoutYear).concat(
    citationTitleVariants(c.ref_title, includeWithoutYear));
  return requested.some(key => candidates.indexOf(key) >= 0);
}

function articleNoParts(no) {
  const text = String(no || "").trim();
  if (text === "全文") return { fullText: true, value: null, suffix: null };
  const m = text.match(/^第([一二三四五六七八九十百千零〇\d]+)条(?:之([一二三四五六七八九十百千零〇\d]+))?(?:[（(][^（）()]+[）)])?$/);
  if (!m) return null;
  // 阿拉伯数字与中文混写（第19十一条、第十九条之1一）无法可靠还原。
  if (/\d/.test(m[1]) && /[一二三四五六七八九十百千零〇]/.test(m[1])) return null;
  if (m[2] && /\d/.test(m[2]) && /[一二三四五六七八九十百千零〇]/.test(m[2])) return null;
  const value = cnToInt(m[1]), suffix = m[2] ? cnToInt(m[2]) : null;
  if (!Number.isFinite(value) || value <= 0 || (m[2] && (!Number.isFinite(suffix) || suffix <= 0))) {
    return null;
  }
  return { fullText: false, value: value, suffix: suffix };
}

function parseCiteText(text) {
  const m = String(text || "").match(/^【(.+?)】$/);
  if (!m) return null;
  const alternatives = [];
  // 分号各项拥有独立 title；每项 rest 可再用顿号列出多个条号。
  m[1].split(/[；;]/).forEach(item => {
    const m2 = item.trim().match(/^(.+?)·(.+)$/);
    if (!m2) return;
    const title = m2[1].trim(), rest = m2[2].trim();
    rest.split("、").forEach(part => {
      const no = part.trim();
      if (no) alternatives.push({ title: title, no: no });
    });
  });
  if (!alternatives.length) return null;
  // 保留原 title/no 接口；完整 alternatives 供严格逐项匹配。
  return {
    title: alternatives[0].title,
    no: alternatives[0].no,
    alternatives: alternatives,
  };
}

function citationTargetKey(c) {
  const law = c.law_id == null ? "title:" + citationTitleKey(c.title || c.ref_title)
    : "law:" + c.law_id;
  return law + ":" + (c.article_id == null ? "law-only" : "article:" + c.article_id);
}

function findCitationSingle(citations, parsed) {
  if (!parsed || !citations || !citations.length) return null;
  const requested = articleNoParts(parsed.no);
  if (!requested) return null;
  // 第一级：保留年份的完整标题与明确别名，不去版本。
  const fullCandidates = citations.filter(c => citationTitleMatches(c, parsed.title, false));
  // 只有正文未带年份且完整标题零候选时，才允许去年份的第二级变体。
  const allowYear = !citationTitleHasYear(parsed.title) && fullCandidates.length === 0;
  const titleCandidates = allowYear
    ? citations.filter(c => citationTitleMatches(c, parsed.title, true)) : fullCandidates;
  const exact = titleCandidates.filter(c => {
    const candidate = articleNoParts(c.ref_no || c.article_no);
    if (!candidate || candidate.fullText !== requested.fullText) return false;
    if (requested.fullText) return true;
    if (candidate.value !== requested.value) return false;
    if (candidate.suffix !== requested.suffix) return false;
    return true;
  });
  if (!exact.length) return null;
  const target = citationTargetKey(exact[0]);
  for (const c of exact) {
    // 多版本或多条号候选指向不同目标时有歧义，不能 exact[0] 猜测。
    if (citationTargetKey(c) !== target) return null;
  }
  return exact[0];
}

function findCitation(citations, parsed) {
  if (!parsed || !citations || !citations.length) return null;
  const alternatives = parsed.alternatives && parsed.alternatives.length
    ? parsed.alternatives : [parsed];
  for (const alternative of alternatives) {
    const hit = findCitationSingle(citations, alternative);
    if (hit) return hit;
  }
  return null;
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
function safeStorageSet(key, value) {
  try { localStorage.setItem(key, value); return true; }
  catch (_) { return false; }
}
function safeStorageRemove(key) {
  try { localStorage.removeItem(key); return true; }
  catch (_) { return false; }
}
// source_url 来自爬取页面的属性，可能是被投毒的 javascript: 伪协议，仅放行 http(s)
function safeUrl(u) {
  return /^https?:\/\//i.test(String(u || "")) ? u : "#";
}

// ---------- 统一启动与事件绑定 ----------
function submitGlobalSearch() {
  const q = $("globalSearch").value.trim();
  navigate(makeHash("/search", q ? { q: q } : {}));
}

function submitSearchPage() {
  const route = parseRoute();
  const q = $("searchPageInput").value.trim();
  navigateWith(route, { q: q || null, law_page: null, article_page: null });
}

function startApp() {
  $("globalSearch").addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    submitGlobalSearch();
  });
  $("btnGlobalSearch").onclick = submitGlobalSearch;
  $("homeSearch").addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    const q = e.target.value.trim();
    navigate(makeHash("/search", q ? { q: q } : {}));
  });
  $("btnHomeSearch").onclick = () => {
    const q = $("homeSearch").value.trim();
    navigate(makeHash("/search", q ? { q: q } : {}));
  };
  $("searchPageInput").addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    submitSearchPage();
  });
  $("btnSearchPage").onclick = submitSearchPage;
  document.querySelectorAll("#searchTabs [data-scope]").forEach(tab => {
    tab.onclick = () => navigateWith(parseRoute(), { scope: tab.dataset.scope });
  });
  [["filterLevel", "level"], ["filterStatus", "status"],
    ["filterSource", "source"], ["filterTopic", "topic"]].forEach(item => {
    $(item[0]).onchange = () => {
      const changes = { law_page: null, article_page: null };
      changes[item[1]] = $(item[0]).value || null;
      navigateWith(parseRoute(), changes);
    };
  });
  $("btnClearFilters").onclick = () => navigateWith(parseRoute(), {
    level: null, status: null, source: null, topic: null,
    law_page: null, article_page: null,
  });
  $("btnRetryFilters").onclick = () => {
    const route = parseRoute();
    if (route.name === "search") {
      renderSearchFilters(route, appStats || {});
      loadSearchFilterOptions(route, routeSerial);
    }
  };
  $("btnNavToggle").onclick = () => {
    const collapsed = $("sidebar").classList.toggle("collapsed");
    $("btnNavToggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
  };
  $("btnSettings").onclick = openSettings;
  $("btnCloseCfg").onclick = closeSettings;
  document.addEventListener("keydown", handleSettingsKeydown);
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
  $("btnStopChat").onclick = stopChat;
  $("chatScope").onchange = () => updateChatScope(null);
  $("chatInput").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      if (e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      sendChat();
    }
  });
  $("btnClearChat").onclick = clearChat;
  $("btnAskLaw").onclick = () => {
    updateChatScope(parseRoute());
    if (currentLawId == null) return;
    $("chatScope").value = "current";
    updateChatScope(parseRoute());
    toggleChat(true);
    $("chatInput").focus();
  };
  $("btnEvidenceClose").onclick = () => closeEvidence(true);
  $("evidenceBackdrop").onclick = () => closeEvidence(true);
  $("evidenceOpenReader").onclick = e => {
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    const hash = $("evidenceOpenReader").getAttribute("href");
    closeEvidence(false);
    navigate(hash);
  };
  document.addEventListener("keydown", handleEvidenceKeydown);
  $("articleSearchInput").addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    searchLawArticles();
  });
  $("btnArticleSearch").onclick = searchLawArticles;
  $("btnTocToggle").onclick = () => {
    const collapsed = $("readerToc").classList.toggle("collapsed");
    $("btnTocToggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
  };
  $("btnReaderSideToggle").onclick = openReaderSide;
  $("btnReaderSideClose").onclick = () => closeReaderSide(true);
  $("readerSideBackdrop").onclick = () => closeReaderSide(true);
  document.addEventListener("keydown", handleReaderSideKeydown);
  window.addEventListener("resize", handleReaderSideResize);
  document.querySelectorAll('#readerTabs [role="tab"]').forEach(tab => {
    tab.onclick = () => activateReaderTab(tab.id);
    tab.addEventListener("keydown", handleReaderTabKeydown);
  });
  $("btnBack").onclick = () => {
    if (history.length > 1) history.back();
    else navigate("/laws");
  };
  $("btnWiki").onclick = () => generateWiki($("wikiBox").dataset.cached === "1");
  document.addEventListener("click", handleInternalLinkClick);
  window.addEventListener("hashchange", handleRoute);
  initChatPanel();
  ensureStats().catch(() => { $("statBadge").textContent = "统计加载失败"; });
  handleRoute();
}

document.addEventListener("DOMContentLoaded", startApp, { once: true });
