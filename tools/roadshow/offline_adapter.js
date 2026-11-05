/* tools/roadshow/offline_adapter.js — 精选 fixture 的完全离线 API 适配器
 * 由构建器在 window.OFFLINE_FIXTURE 之后、原 web/app.js 之前加载。
 * 仅实现数据接口，不操作 DOM；所有响应均来自本地 fixture，不发起网络请求。
 * 语法保持 ES2017，供无现代语法支持的内网浏览器使用。
 */
(function (window) {
  "use strict";

  var fixture = window.OFFLINE_FIXTURE || {};
  var BASE_URL = "http://offline.local";
  var laws = fixture.laws || {};
  var articlesByLaw = fixture.articles || {};
  var articleById = fixture.article_by_id || {};
  var relations = fixture.relations || [];
  var chats = fixture.chats || {};
  var wikis = fixture.wikis || {};
  window.offlineErrors = window.offlineErrors || [];

  function headerValue(headers, name) {
    if (!headers) return null;
    if (typeof headers.get === "function") return headers.get(name);
    var wanted = name.toLowerCase();
    var keys = Object.keys(headers);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].toLowerCase() === wanted) return headers[keys[i]];
    }
    return null;
  }

  function makeHeaders(values) {
    var copied = {};
    Object.keys(values || {}).forEach(function (key) {
      copied[key] = values[key];
    });
    return {
      get: function (name) { return headerValue(copied, name); },
      has: function (name) { return headerValue(copied, name) !== null; },
    };
  }

  function utf8Bytes(text) {
    if (typeof window.TextEncoder === "function") {
      return new window.TextEncoder().encode(text);
    }
    // encodeURIComponent 的 %XX 序列可无损还原 UTF-8；保留未转义的 ASCII 字节。
    var encoded = unescape(encodeURIComponent(text));
    var bytes = new Uint8Array(encoded.length);
    for (var i = 0; i < encoded.length; i++) bytes[i] = encoded.charCodeAt(i);
    return bytes;
  }

  // 自带最小 Response 兼容层，避免 file:// 页面或旧浏览器依赖原生 Response。
  function makeResponse(bodyText, status, contentType) {
    var consumed = false;
    var cancelled = false;
    var response = {
      status: status,
      ok: status >= 200 && status < 300,
      statusText: status === 200 ? "OK" : (status === 404 ? "Not Found" : "Error"),
      url: "",
      headers: makeHeaders({ "Content-Type": contentType + "; charset=utf-8" }),
      text: function () {
        consumed = true;
        return Promise.resolve(bodyText);
      },
      json: function () {
        return response.text().then(function (text) { return JSON.parse(text); });
      },
      clone: function () { return makeResponse(bodyText, status, contentType); },
    };
    response.body = {
      getReader: function () {
        var done = false;
        return {
          read: function () {
            if (done || cancelled || consumed) {
              return Promise.resolve({ done: true, value: undefined });
            }
            done = true;
            consumed = true;
            return Promise.resolve({ done: false, value: utf8Bytes(bodyText) });
          },
          cancel: function () {
            cancelled = true;
            return Promise.resolve();
          },
        };
      },
    };
    return response;
  }

  function jsonResponse(payload, status) {
    return makeResponse(JSON.stringify(payload), status || 200, "application/json");
  }

  function ndjsonResponse(events) {
    var lines = events.map(function (event) { return JSON.stringify(event); });
    return makeResponse(lines.join("\n") + "\n", 200, "application/x-ndjson");
  }

  function positiveInt(value, fallback, maximum) {
    if (value === null || value === undefined || value === "") return fallback;
    var parsed = Number(value);
    if (!isFinite(parsed) || parsed < 1) return fallback;
    parsed = Math.floor(parsed);
    return maximum ? Math.min(parsed, maximum) : parsed;
  }

  // 所有列表型接口共用分页语义；top_k 仅决定 search 的有效 page_size。
  function paginate(items, params, defaultSize, sizeOverride) {
    var page = positiveInt(params.get("page"), 1);
    var pageSize = positiveInt(sizeOverride || params.get("page_size"), defaultSize, 100);
    var offset = (page - 1) * pageSize;
    var pageItems = items.slice(offset, offset + pageSize);
    return {
      items: pageItems,
      total: items.length,
      page: page,
      page_size: pageSize,
      has_more: offset + pageItems.length < items.length,
    };
  }

  function queryTerms(query) {
    var seen = {};
    return String(query || "").trim().split(/\s+/).filter(function (term) {
      if (!term || seen[term]) return false;
      seen[term] = true;
      return true;
    });
  }

  function includesTerm(text, term) {
    return String(text || "").toLowerCase().indexOf(term.toLowerCase()) >= 0;
  }

  function containsEveryTerm(text, terms) {
    for (var i = 0; i < terms.length; i++) {
      if (!includesTerm(text, terms[i])) return false;
    }
    return true;
  }

  function lawArticles(id) {
    return articlesByLaw[String(id)] || [];
  }

  function listLaw(law) {
    return {
      id: law.id,
      title: law.title,
      doc_number: law.doc_number,
      issuer: law.issuer,
      level: law.level,
      topic_tags: law.topic_tags,
      issue_date: law.issue_date,
      effective_date: law.effective_date,
      status: law.status,
      source_name: law.source_name,
      source_url: law.source_url,
      updated_at: law.updated_at,
    };
  }

  function topicMatches(law, topic) {
    if (topic === null) return true;
    var wanted = String(topic || "").trim().toLowerCase();
    if (!wanted) return false;
    var tags = String(law.topic_tags || "").split(/[,，;；]/);
    for (var i = 0; i < tags.length; i++) {
      if (tags[i].trim().toLowerCase() === wanted) return true;
    }
    return false;
  }

  function lawMatchesFilters(law, params) {
    var level = params.get("level"), status = params.get("status");
    var source = params.get("source"), topic = params.get("topic");
    return (!level || law.level === level) &&
      (!status || law.status === status) &&
      (!source || law.source_name === source) &&
      topicMatches(law, topic);
  }

  function lawText(law) {
    var parts = [law.title || ""];
    lawArticles(law.id).forEach(function (article) {
      parts.push(article.article_no || "", article.text || "");
    });
    return parts.join("\n");
  }

  function sortedLawIds() {
    return Object.keys(laws).sort(function (a, b) { return Number(a) - Number(b); });
  }

  function listLaws(url) {
    var params = url.searchParams, terms = queryTerms(params.get("q"));
    var selected = [];
    sortedLawIds().forEach(function (id) {
      var law = laws[id];
      if (lawMatchesFilters(law, params) && containsEveryTerm(lawText(law), terms)) {
        selected.push(law);
      }
    });
    if (params.get("sort") === "updated") {
      selected.sort(function (a, b) {
        var aDate = a.updated_at || a.created_at || "";
        var bDate = b.updated_at || b.created_at || "";
        return aDate === bDate ? b.id - a.id : (aDate < bDate ? 1 : -1);
      });
    } else {
      selected.sort(function (a, b) { return b.id - a.id; });
    }
    var data = paginate(selected, params, 20);
    return jsonResponse({
      total: data.total,
      page: data.page,
      page_size: data.page_size,
      items: data.items.map(listLaw),
    });
  }

  function publicArticle(article) {
    return { id: article.id, article_no: article.article_no, text: article.text };
  }

  var CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
  };

  function chineseNumber(value) {
    if (/^\d+$/.test(value)) return parseInt(value, 10);
    if (!/^[零〇一二两三四五六七八九十百千]+$/.test(value)) return null;
    var units = { "十": 10, "百": 100, "千": 1000 };
    var total = 0, current = 0;
    for (var i = 0; i < value.length; i++) {
      var char = value.charAt(i);
      if (Object.prototype.hasOwnProperty.call(units, char)) {
        var unit = units[char];
        if (current === 0) current = 1;
        total += current * unit;
        current = 0;
      } else {
        current = CN_DIGITS[char];
      }
    }
    return total + current;
  }

  function normalizeArticleNo(value) {
    var raw = String(value || "").trim();
    var match = raw.match(/^第([零〇一二两三四五六七八九十百千\d]+)条$/);
    if (!match) return raw;
    var number = chineseNumber(match[1]);
    return number === null ? null : number;
  }

  function findArticlePosition(articles, articleNo) {
    if (!articleNo) return -1;
    var raw = String(articleNo).trim();
    var normalized = normalizeArticleNo(raw);
    if (normalized === null) return -1;
    for (var i = 0; i < articles.length; i++) {
      if (String(articles[i].article_no || "").trim() === raw) return i;
    }
    for (var j = 0; j < articles.length; j++) {
      if (normalizeArticleNo(articles[j].article_no) === normalized) return j;
    }
    return -1;
  }

  function articlePage(id, url, includeQuery) {
    var params = url.searchParams;
    var pageSize = positiveInt(params.get("page_size"), 50, 100);
    var page = positiveInt(params.get("page"), 1);
    var allArticles = lawArticles(id);
    var selected = allArticles;
    var articleNo = params.get("article_no");
    var targetId = null;
    var targetPosition = findArticlePosition(allArticles, articleNo);
    if (targetPosition >= 0) {
      targetId = allArticles[targetPosition].id;
      page = Math.floor(targetPosition / pageSize) + 1;
    }
    var query = includeQuery && !articleNo ? params.get("q") : null;
    if (query) {
      var terms = queryTerms(query);
      selected = allArticles.filter(function (article) {
        return containsEveryTerm((article.article_no || "") + "\n" + (article.text || ""), terms);
      });
    }
    var offset = (page - 1) * pageSize;
    return {
      articles: selected.slice(offset, offset + pageSize).map(publicArticle),
      article_index: allArticles.map(function (article) {
        return { id: article.id, article_no: article.article_no };
      }),
      article_total: selected.length,
      article_page: page,
      article_page_size: pageSize,
      target_article_id: targetId,
    };
  }

  function reader(id, url) {
    var data = articlePage(id, url, false);
    data.law = laws[String(id)];
    data.wiki = wikis[String(id)] || null;
    return jsonResponse(data);
  }

  function relationEndpoint(relation, side) {
    // 精选条文优先提供实时字段；未选入条文仍使用 fixture 保存的真实 JOIN 元数据。
    var article = articleById[String(relation[side + "_id"])] || {};
    var lawId = article.law_id != null ? article.law_id : relation[side + "_law_id"];
    return {
      article: article,
      lawId: lawId,
      law: laws[String(lawId)] || {},
    };
  }

  function relationView(relation) {
    var from = relationEndpoint(relation, "from");
    var to = relationEndpoint(relation, "to");
    return {
      rel_type: relation.rel_type,
      note: relation.note,
      from_id: relation.from_id,
      to_id: relation.to_id,
      from_law_id: from.lawId,
      to_law_id: to.lawId,
      to_law_title: to.law.title || relation.to_law_title || "",
      to_article_no: to.article.article_no || relation.to_article_no || "",
      from_law_title: from.law.title || relation.from_law_title || "",
      from_article_no: from.article.article_no || relation.from_article_no || "",
    };
  }

  function lawRelations(id) {
    var outgoing = [], incoming = [];
    relations.forEach(function (relation) {
      var from = relationEndpoint(relation, "from");
      var to = relationEndpoint(relation, "to");
      if (Number(from.lawId) === id) outgoing.push(relationView(relation));
      if (Number(to.lawId) === id) incoming.push(relationView(relation));
    });
    return jsonResponse({ outgoing: outgoing, incoming: incoming });
  }

  function countTerm(text, term) {
    var source = String(text || "").toLowerCase();
    var needle = term.toLowerCase(), count = 0, at = 0;
    while (needle && (at = source.indexOf(needle, at)) >= 0) {
      count++;
      at += needle.length;
    }
    return count;
  }

  function makeSnippet(text, terms) {
    var source = String(text || "").replace(/\s+/g, " ").trim();
    var first = -1;
    terms.forEach(function (term) {
      var at = source.toLowerCase().indexOf(term.toLowerCase());
      if (at >= 0 && (first < 0 || at < first)) first = at;
    });
    if (source.length <= 260) return source;
    var start = Math.max(0, (first < 0 ? 0 : first) - 100);
    start = Math.min(start, source.length - 260);
    return (start ? "…" : "") + source.slice(start, start + 260) +
      (start + 260 < source.length ? "…" : "");
  }

  function search(url) {
    var params = url.searchParams, query = params.get("q") || "";
    var terms = queryTerms(query), candidates = [];
    if (terms.length) {
      sortedLawIds().forEach(function (lawId) {
        var law = laws[lawId];
        if (!lawMatchesFilters(law, params)) return;
        lawArticles(lawId).forEach(function (article) {
          var titleAndText = (law.title || "") + "\n" + (article.text || "");
          if (!containsEveryTerm(titleAndText, terms)) return;
          var score = 0;
          terms.forEach(function (term) {
            // 仅用于离线演示排序，不是 BM25、向量相似度或生产检索分数。
            score += countTerm(article.text, term) + countTerm(law.title, term) * 3;
          });
          candidates.push({
            id: article.id,
            law_id: law.id,
            article_no: article.article_no,
            text: article.text,
            title: law.title,
            level: law.level || "",
            status: law.status || "",
            source_url: law.source_url || "",
            score: score,
            match_type: "keyword",
            match_reasons: ["keyword"],
            highlight_terms: terms.slice(),
            snippet: makeSnippet(article.text, terms),
          });
        });
      });
    }
    candidates.sort(function (a, b) {
      return b.score - a.score || a.law_id - b.law_id || a.id - b.id;
    });
    var topK = params.get("top_k");
    var data = paginate(candidates, params, 10, topK);
    return jsonResponse({
      query: query,
      count: data.items.length,
      results: data.items,
      total: data.total,
      page: data.page,
      page_size: data.page_size,
      has_more: data.has_more,
      mode: "offline-demo",
    });
  }

  function lastUserMessage(body) {
    var messages = body && Array.isArray(body.messages) ? body.messages : [];
    for (var i = messages.length - 1; i >= 0; i--) {
      if (messages[i] && messages[i].role === "user") return String(messages[i].content || "");
    }
    return "";
  }

  function copyChat(preset) {
    return {
      content: preset.content,
      searches: (preset.searches || []).slice(),
      citations: (preset.citations || []).map(function (citation) {
        return Object.assign({}, citation);
      }),
    };
  }

  function normalizeQuestion(value) {
    return String(value || "").trim().replace(/\s+/g, "");
  }

  function matchingChat(body) {
    var question = normalizeQuestion(lastUserMessage(body));
    if (!question) return null;
    var keys = Object.keys(chats);
    for (var i = 0; i < keys.length; i++) {
      var preset = chats[keys[i]];
      if (preset && normalizeQuestion(preset.question) === question) return preset;
    }
    return null;
  }

  var OFFLINE_CHAT_ERROR = "离线演示仅支持预置问题。请尝试：" +
    (chats.reduction && chats.reduction.question ? "“" + chats.reduction.question + "”" : "减持预置问题") +
    "，或" +
    (chats.disclosure && chats.disclosure.question ? "“" + chats.disclosure.question + "”" : "信息披露预置问题") +
    "。该问题未预置，无法调用真实大模型，不会生成虚构回答。";

  function chatScopeError(body, preset) {
    if (!body || body.law_id === null || body.law_id === undefined ||
        String(body.law_id).trim() === "") return null;
    var lawId = String(body.law_id).trim();
    var citations = preset.citations || [];
    var allowed = citations.length > 0 && citations.every(function (citation) {
      return String(citation.law_id) === lawId;
    });
    return allowed ? null : "当前法规未预置该问题。" + OFFLINE_CHAT_ERROR;
  }

  function chatResponse(body, stream) {
    var preset = matchingChat(body);
    var error = preset ? chatScopeError(body, preset) : OFFLINE_CHAT_ERROR;
    if (error) {
      if (stream) return ndjsonResponse([{ type: "error", error: error }]);
      // 原 JSON 客户端通过 j() 读取非 2xx 的 detail；error 同步保留给直接 API 调用者。
      return jsonResponse({ error: error, detail: error }, 400);
    }
    var result = copyChat(preset);
    if (!stream) return jsonResponse(result);
    var searchQuery = result.searches.length ? result.searches[0] : "预置问题";
    return ndjsonResponse([
      { type: "stage", stage: "thinking", round: 1 },
      { type: "stage", stage: "searching", round: 1, query: searchQuery },
      { type: "stage", stage: "citing", round: 1 },
      { type: "result", data: result },
    ]);
  }

  function settings() {
    return {
      api_base: "离线演示",
      api_key_masked: "",
      has_key: false,
      chat_model: "",
      embedding_model: "",
      temperature: 0.1,
    };
  }

  function parseBody(init) {
    if (!init || init.body === null || init.body === undefined || init.body === "") return {};
    if (typeof init.body === "object") return init.body;
    try { return JSON.parse(init.body); }
    catch (_) { return {}; }
  }

  function notFound(message) {
    return jsonResponse({ detail: message || "法规不存在" }, 404);
  }

  function uncovered(method, url) {
    var message = "离线演示未提供该接口";
    window.offlineErrors.push({
      method: method,
      path: url.pathname + url.search,
      error: message,
    });
    return jsonResponse({ error: message }, 404);
  }

  function route(method, url, init) {
    var path = url.pathname, match;
    if (method === "GET" && path === "/api/stats") return jsonResponse(fixture.stats || {});
    if (method === "GET" && path === "/api/laws") return listLaws(url);

    match = path.match(/^\/api\/laws\/([^/]+)$/);
    if (method === "GET" && match) {
      var id = /^\d+$/.test(match[1]) ? Number(match[1]) : NaN;
      if (!isFinite(id) || !laws[String(id)]) return notFound("法规不存在");
      if ((url.searchParams.get("mode") || "reader") === "reader") return reader(id, url);
      return jsonResponse({
        law: laws[String(id)],
        articles: lawArticles(id).map(publicArticle),
        wiki: wikis[String(id)] || null,
      });
    }

    match = path.match(/^\/api\/laws\/([^/]+)\/articles$/);
    if (method === "GET" && match) {
      var articleLawId = /^\d+$/.test(match[1]) ? Number(match[1]) : NaN;
      if (!isFinite(articleLawId) || !laws[String(articleLawId)]) return notFound("法规不存在");
      return jsonResponse(articlePage(articleLawId, url, true));
    }

    match = path.match(/^\/api\/laws\/([^/]+)\/relations$/);
    if (method === "GET" && match) {
      var relationLawId = /^\d+$/.test(match[1]) ? Number(match[1]) : NaN;
      if (!isFinite(relationLawId) || !laws[String(relationLawId)]) return notFound("法规不存在");
      return lawRelations(relationLawId);
    }

    if (method === "GET" && path === "/api/search") return search(url);
    if (method === "GET" && path === "/api/graph") return jsonResponse(fixture.graph || { nodes: [], edges: [] });
    if (method === "POST" && path === "/api/chat/stream") return chatResponse(parseBody(init), true);
    if (method === "POST" && path === "/api/chat") return chatResponse(parseBody(init), false);

    match = path.match(/^\/api\/laws\/([^/]+)\/wiki$/);
    if (method === "POST" && match) {
      var wikiLawId = /^\d+$/.test(match[1]) ? Number(match[1]) : NaN;
      if (!isFinite(wikiLawId) || !laws[String(wikiLawId)]) return notFound("法规不存在");
      if (url.searchParams.get("force") === "true") {
        return jsonResponse({ error: "完全离线演示不重新生成解读" });
      }
      return jsonResponse(wikis[String(wikiLawId)] ||
        { error: "该法规没有预生成解读，完全离线演示不重新生成解读" });
    }

    if (method === "GET" && path === "/api/settings") return jsonResponse(settings());
    if (method === "PUT" && path === "/api/settings") {
      return jsonResponse({ ok: false, error: "完全离线演示不保存配置，也不会写入真实配置文件" });
    }
    if (method === "POST" && path === "/api/settings/test") {
      return jsonResponse({ ok: false, error: "完全离线演示不连接或测试真实大模型服务" });
    }
    return uncovered(method, url);
  }

  window.fetch = function (input, init) {
    init = init || {};
    var rawUrl = typeof input === "string" ? input : String(input && input.url || input || "");
    var method = String(init.method || (input && input.method) || "GET").toUpperCase();
    var signal = init.signal || (input && input.signal);
    if (signal && signal.aborted) {
      return Promise.reject(new Error("The operation was aborted"));
    }
    var url;
    try { url = new URL(rawUrl, BASE_URL); }
    catch (_) {
      url = new URL("/", BASE_URL);
      return Promise.resolve(uncovered(method, url));
    }
    try {
      return Promise.resolve(route(method, url, init));
    } catch (error) {
      return Promise.reject(error);
    }
  };

  // 暴露纯响应构造函数，便于无网络行为测试；业务页面只应使用 window.fetch。
  window.offlineAdapter = {
    jsonResponse: jsonResponse,
    ndjsonResponse: ndjsonResponse,
  };
})(typeof window !== "undefined" ? window : this);
