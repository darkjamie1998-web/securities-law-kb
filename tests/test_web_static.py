"""前端静态契约：离线可检，不启动服务、不依赖 Node。"""
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.tags[values["id"]] = (tag, values)


def _html():
    return (WEB / "index.html").read_text(encoding="utf-8")


def test_static_asset_versions_are_consistent():
    versions = re.findall(
        r"/static/(?:style\.css|app\.js|graph\.js)\?v=([A-Za-z0-9._-]+)",
        _html(),
    )
    assert len(versions) == 3
    assert versions == ["20261104a"] * 3
    assert "20261103a" not in versions


@pytest.mark.parametrize("filename", ["app.js", "graph.js"])
def test_web_javascript_stays_within_es2017(filename):
    source = (WEB / filename).read_text(encoding="utf-8")
    forbidden = {
        "optional chaining": r"\?\.",
        "nullish coalescing": r"\?\?",
        "trimEnd": r"\.\s*trimEnd\s*\(",
        "object spread/rest": r"(?:\{|\,)\s*\.\.\.\s*[A-Za-z_$]",
    }
    violations = [
        f"{name}: line {source[:match.start()].count(chr(10)) + 1}"
        for name, pattern in forbidden.items()
        for match in re.finditer(pattern, source)
    ]
    assert not violations, "超 ES2017 语法（含注释）：" + "; ".join(violations)


def test_semantic_navigation_search_and_dialog_contract():
    parser = _Ids()
    parser.feed(_html())
    tags = parser.tags

    assert tags["brand"][0] == "a"
    assert tags["brand"][1].get("href") == "#/home"
    assert tags["navGraph"][0] == "a"
    assert tags["navGraph"][1].get("href") == "#/graph"
    assert tags["globalSearch"][0] == "input"
    assert tags["globalSearch"][1].get("type") == "search"
    assert tags["btnGlobalSearch"][0] == "button"
    assert tags["homeSearch"][0] == "input"
    assert tags["homeSearch"][1].get("type") == "search"
    assert tags["btnHomeSearch"][0] == "button"
    assert tags["btnSearchPage"][0] == "button"
    assert tags["btnClearFilters"][0] == "button"

    dialog = tags["settingsModal"][1]
    assert dialog.get("role") == "dialog"
    assert dialog.get("aria-modal") == "true"
    assert dialog.get("aria-labelledby") == "settingsTitle"
    assert "settingsTitle" in tags


def test_search_scope_buttons_and_result_landmarks_are_semantic():
    parser = _Ids()
    parser.feed(_html())
    tags = parser.tags

    for element_id in ("searchLawResults", "searchArticleResults"):
        assert element_id in tags
        assert tags[element_id][1].get("aria-live") == "polite"
    assert tags["searchTabs"][1].get("role") == "group"
    assert len(re.findall(
        r'<button[^>]+data-scope="(?:all|law|article)"[^>]+aria-pressed="(?:true|false)"',
        _html(),
    )) == 3
    search_tabs = re.search(r'<div id="searchTabs"[\s\S]*?</div>', _html()).group(0)
    assert 'role="tab"' not in search_tabs
    assert 'role="tablist"' not in search_tabs


def test_app_loads_before_graph_with_one_deferred_startup():
    html = _html()
    assert html.index("/static/app.js") < html.index("/static/graph.js")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    graph = (WEB / "graph.js").read_text(encoding="utf-8")
    assert 'DOMContentLoaded' in app
    assert "registerRouteHandler" in app
    assert "registerRouteHandler(\"graph\", showGraph)" in graph


def test_detail_restore_graph_resize_and_route_focus_guards():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    graph = (WEB / "graph.js").read_text(encoding="utf-8")
    assert "let navigationIntent = null;" in app
    assert "const isInitial = lastHash === null;" in app
    assert "const isPush = navigationIntent === hash;" in app
    assert "navigationIntent = null;" in app
    assert "if (isPush) delete scrollPositions[hash];" in app
    assert (
        "route.restoreScroll = !isInitial && !isPush && "
        "Object.prototype.hasOwnProperty.call(scrollPositions, hash);"
    ) in app
    assert app.count("navigationIntent = hash;") >= 2
    assert "function handleInternalLinkClick(e)" in app
    link_handler = app.split("function handleInternalLinkClick(e) {", 1)[1].split("\n}", 1)[0]
    assert "e.ctrlKey || e.metaKey || e.shiftKey || e.altKey" in link_handler
    assert 'location.hash || "#/home"' in link_handler
    assert "handleRoute();" in link_handler
    assert "navigate(" not in link_handler
    assert 'document.addEventListener("click", handleInternalLinkClick);' in app
    assert "locateArticle" in app
    assert "expectedHash" in app
    assert "ticket === routeSerial && location.hash === expectedHash" in app
    assert "preventScroll: true" in app
    assert "if (!width || !height) return;" in graph
    assert "if (G) { resizeCanvas(); drawGraph(); return; }" in graph


def test_stats_failure_preserves_route_filters_and_offers_retry():
    parser = _Ids()
    parser.feed(_html())
    assert "filterLoadStatus" in parser.tags
    assert "btnRetryFilters" in parser.tags
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "renderSearchFilters(route, appStats || {})" in app
    assert "loadSearchFilterOptions(route, ticket)" in app
    assert "entries.some" in app
    assert "筛选项加载失败" in app


def test_dialog_focus_trap_safe_highlight_and_storage_helpers():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function settingsFocusable()" in app
    assert 'e.key === "Tab"' in app
    assert "e.shiftKey" in app
    assert "seen = new Set()" in app
    assert "seen.has(word)" in app
    assert "seen.add(word)" in app
    assert len(re.findall(r"localStorage\.setItem\(", app)) == 1
    assert len(re.findall(r"localStorage\.removeItem\(", app)) == 1
    assert "function safeStorageSet(" in app
    assert "function safeStorageRemove(" in app


def test_reader_has_three_regions_and_independent_article_search():
    parser = _Ids()
    parser.feed(_html())
    tags = parser.tags

    for element_id in (
        "readerToc", "articleToc", "articleList", "articleReaderPager",
        "articleSearchInput", "btnArticleSearch", "articleSearchStatus",
        "articleSearchResults", "officialLawLink", "btnAskLaw",
    ):
        assert element_id in tags
    assert tags["readerToc"][0] == "aside"
    assert tags["readerToc"][1].get("aria-label") == "本篇条号目录"
    assert tags["articleToc"][0] == "nav"
    assert tags["articleSearchInput"][1].get("type") == "search"
    assert tags["articleSearchResults"][1].get("aria-live") == "polite"
    assert tags["articleSearchStatus"][1].get("aria-live") == "polite"
    assert tags["btnTocToggle"][1].get("aria-controls") == "readerToc"
    assert tags["officialLawLink"][1].get("rel") == "noopener noreferrer"


def test_reader_tabs_follow_aria_and_keyboard_contract():
    parser = _Ids()
    parser.feed(_html())
    tags = parser.tags

    assert tags["readerTabs"][1].get("role") == "tablist"
    for tab_id, panel_id, selected, tabindex in (
        ("tabWiki", "wikiPanel", "true", "0"),
        ("tabRelations", "relationPanel", "false", "-1"),
    ):
        tab = tags[tab_id][1]
        assert tab.get("role") == "tab"
        assert tab.get("aria-controls") == panel_id
        assert tab.get("aria-selected") == selected
        assert tab.get("tabindex") == tabindex
        panel = tags[panel_id][1]
        assert panel.get("role") == "tabpanel"
        assert panel.get("aria-labelledby") == tab_id
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function activateReaderTab(" in app
    assert "function handleReaderTabKeydown(" in app
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert f'"{key}"' in app
    assert 'tab.setAttribute("tabindex"' in app
    assert 'tab.setAttribute("aria-selected"' in app


def test_reader_api_routes_pagination_and_search_are_explicit():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "lawReader:" in app
    assert "lawArticles:" in app
    assert 'mode: "reader"' in app
    assert "const READER_PAGE_SIZE = 50;" in app
    detail = app.split("async function loadLawDetail(", 1)[1].split(
        "// ---------- 关联法规", 1)[0]
    assert "API.lawReader(" in detail
    assert "API.law(id)" not in detail
    assert "article_no: null" in app
    assert "function searchLawArticles(" in app
    assert "API.lawArticles(" in app
    assert "articleSearchSerial" in app
    assert "articleSearchResults" in app


def test_reader_copy_fallback_fulltext_and_stale_response_guards():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'item.id = "art-" + a.id;' not in app
    assert "return a.id == null ||" in app
    assert "navigator.clipboard" in app
    assert "document.execCommand(\"copy\")" in app
    assert "复制成功" in app
    assert "复制失败" in app
    assert "generated_at" in app
    assert "model_used" in app
    assert "confirm(" in app
    assert "token" in app
    assert "function readerRouteIsCurrent(" in app
    assert "ticket === routeSerial && location.hash === expectedHash" in app
    assert "route.name === \"law\" && route.id === id" in app
    assert "recordRecentLaw(id, law.title)" in app


def test_narrow_reader_side_drawer_has_accessible_lifecycle():
    parser = _Ids()
    parser.feed(_html())
    tags = parser.tags

    toggle = tags["btnReaderSideToggle"][1]
    assert toggle.get("aria-controls") == "readerSide"
    assert toggle.get("aria-expanded") == "false"
    assert tags["readerSide"][0] == "aside"
    assert tags["btnReaderSideClose"][1].get("aria-controls") == "readerSide"
    assert "readerSideBackdrop" in tags
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function openReaderSide(" in app
    assert "function closeReaderSide(" in app
    assert "function handleReaderSideKeydown(" in app
    route_handler = app.split("async function handleRoute() {", 1)[1].split(
        "const route = parseRoute();", 1)[0]
    assert "closeReaderSide();" in route_handler
    assert 'e.key === "Escape"' in app
    assert 'setAttribute("aria-expanded", "true")' in app
    assert 'setAttribute("aria-expanded", "false")' in app
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".reader-side.drawer-open" in css
    assert ".reader-side-backdrop" in css
    assert "#btnReaderSideToggle" in css


def test_unsplit_real_id_fulltext_location_and_independent_hit_expiry():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    toc = app.split("function renderReaderToc(", 1)[1].split(
        "function changeReaderPage(", 1)[0]
    articles = app.split("function renderReaderArticles(", 1)[1].split(
        "async function loadLawDetail(", 1)[0]
    assert "function isReaderFulltextArticle(" in app
    assert "isReaderFulltextArticle(data, a)" in toc
    assert "isReaderFulltextArticle(data, a)" in articles
    assert 'highlightNo === "全文"' in toc
    assert 'highlightNo === "全文"' in articles
    assert 'item.id = fullText ? "art-full"' in articles
    detail = app.split("async function loadLawDetail(", 1)[1].split(
        "function articleSnippet(", 1)[0]
    assert "if (target) {\n    if (locateArticle) {" in detail
    assert 'target.classList.remove("hit")' in detail


def test_chat_multiline_scope_and_ask_current_law_controls():
    parser = _Ids()
    parser.feed(_html())
    tags = parser.tags

    assert tags["chatInput"][0] == "textarea"
    assert tags["chatScope"][0] == "select"
    scope = re.search(r'<select id="chatScope"[\s\S]*?</select>', _html()).group(0)
    assert '<option value="all">全库</option>' in scope
    assert re.search(r'<option value="current"[^>]*disabled[^>]*>当前法规</option>', scope)
    assert tags["btnStopChat"][0] == "button"
    assert "即将支持" not in tags["btnAskLaw"][1].get("title", "")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function updateChatScope(" in app
    assert "updateChatScope(route);" in app
    assert '$("btnAskLaw").onclick' in app
    assert 'e.key === "Enter" && !e.shiftKey' in app
    assert "e.preventDefault();" in app.split('$("chatInput").addEventListener("keydown"', 1)[1].split("});", 1)[0]


def test_chat_stream_parser_real_stages_fallback_and_stop_retry_contract():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'fetch("/api/chat/stream"' in app
    stream_api = app.split("chatStream:", 1)[1].split("wiki:", 1)[0]
    assert ".then(j)" not in stream_api
    assert "signal" in stream_api and "law_id" in stream_api
    assert "function supportsChatStream(" in app
    assert "ReadableStream" in app and "TextDecoder" in app
    assert "function readChatStream(" in app
    parser = app.split("async function readChatStream(", 1)[1].split(
        "async function requestChat(", 1)[0]
    assert "response.body.getReader()" in parser
    assert "decoder.decode(value, { stream: true })" in parser
    assert "decoder.decode()" in parser
    assert "JSON.parse" in parser
    assert "return API.chat(" in app.split("async function requestChat(", 1)[1].split(
        "function chatStageText(", 1)[0]
    for stage in ("thinking", "searching", "search_done", "citing"):
        assert f'{stage}:' in app
    for text in ("正在思考", "正在检索", "检索完成", "正在整理引用"):
        assert text in app
    assert "function stopChat(" in app
    assert "new AbortController()" in app
    assert ".abort()" in app
    assert "当前已发起的模型请求可能仍需结束后才能停止" in app
    assert "function retryChat(" in app
    retry = app.split("async function retryChat(", 1)[1].split(
        "async function sendChat(", 1)[0]
    assert "appendMsg(\"user\"" not in retry
    assert 'chatHistory.push({ role: "user"' not in retry
    assert 'chatHistory.push({ role: "assistant", content: r.content })' in app
    assert "function clearChat(" in app
    clear = app.split("function clearChat(", 1)[1].split(
        "function appendMsg(", 1)[0]
    assert "confirm(" in clear
    assert clear.index("stopChat(") < clear.index("chatHistory = [];")


def test_chat_capability_preflight_and_text_fallback_never_reposts():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    supports = app.split("function supportsChatStream(", 1)[1].split(
        "async function readChatStream(", 1)[0]
    assert "ReadableStream.prototype" in supports
    assert 'typeof ReadableStream.prototype.getReader === "function"' in supports
    assert 'typeof TextDecoder === "function"' in supports
    assert 'typeof AbortController === "function"' in supports

    request = app.split("async function requestChat(", 1)[1].split(
        "function chatStageText(", 1)[0]
    # 只有请求前的能力不足分支可以调用旧 JSON；流请求发出后禁止第二次 POST。
    before_post, after_post = request.split("const response = await API.chatStream", 1)
    assert "return API.chat(" in before_post
    assert "API.chat(" not in after_post
    assert request.count("API.chatStream(") == 1
    assert "return readChatStream(response, onStage);" in after_post

    reader = app.split("async function readChatStream(", 1)[1].split(
        "async function requestChat(", 1)[0]
    # 响应意外无 body/getReader 时读取完整 NDJSON，并复用 handleLine 的
    # result/error/cancelled 处理，而不是重新请求旧 /api/chat。
    assert "response.body && typeof response.body.getReader" in reader
    assert "response.body.getReader()" in reader
    assert "buffer = await response.text();" in reader
    assert reader.count("for (const line of lines) handleLine(line);") == 2
    assert "JSON.parse" in reader
    assert 'event.type === "result"' in reader
    assert 'event.type === "error"' in reader
    assert 'event.type === "cancelled"' in reader
    assert "API.chat(" not in reader


def test_chat_result_citations_open_evidence_without_guessing_or_double_escaping():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function renderChatResult(" in app
    assert "function bindCitationActions(" in app
    rendered = app.split("function renderChatResult(", 1)[1].split(
        "function bindCitationActions(", 1)[0]
    assert "renderMarkdown(r.content)" in rendered
    assert "esc(r.content)" not in rendered
    assert "safeUrl(c.source_url)" in rendered
    assert 'target="_blank" rel="noopener noreferrer"' in rendered
    assert "cite-unmatched" in rendered
    bound = app.split("function bindCitationActions(", 1)[1].split(
        "function stopChat(", 1)[0]
    assert "openEvidence(hit)" in bound
    assert "openEvidence(citation)" in bound
    assert "parseCiteText" in bound and "findCitation" in bound
    assert "cite-flash" in bound
    assert "stopPropagation()" in bound
    assert "navigate(" not in bound
    assert "hit.law_id," not in bound


def test_evidence_drawer_semantics_reader_targeting_and_focus_lifecycle():
    parser = _Ids()
    parser.feed(_html())
    tags = parser.tags
    drawer = tags["evidenceDrawer"][1]
    assert drawer.get("role") == "dialog"
    assert drawer.get("aria-modal") == "true"
    assert drawer.get("aria-labelledby") == "evidenceTitle"
    assert "evidenceTitle" in tags and "evidenceBackdrop" in tags
    assert tags["evidenceBody"][1].get("aria-live") == "polite"
    assert tags["evidenceOpenReader"][0] == "a"
    assert tags["evidenceOpenReader"][1].get("href", "").startswith("#/")
    assert tags["btnEvidenceClose"][0] == "button"

    app = (WEB / "app.js").read_text(encoding="utf-8")
    evidence = app.split("async function openEvidence(", 1)[1].split(
        "// ---------- 解读 / 关联 tabs", 1)[0]
    assert "let evidenceSerial = 0;" in app
    assert "ticket !== evidenceSerial" in evidence
    assert "API.lawReader(c.law_id" in evidence
    assert "c.article_id != null ? c.article_no : null" in evidence
    assert "a.id === c.article_id" in evidence
    assert "条号未定位" in evidence
    assert "safeUrl(" in evidence
    assert "evidenceReturnFocus" in app
    assert "function evidenceFocusable(" in app
    assert "function handleEvidenceKeydown(" in app
    assert 'e.key === "Tab"' in evidence or 'e.key === "Tab"' in app
    assert "function closeEvidence(" in app
    reader = app.split('$("evidenceOpenReader").onclick', 1)[1].split("};", 1)[0]
    assert reader.index("closeEvidence(") < reader.index("navigate(")
    assert "c.article_id != null ? { article_no: c.article_no } : {}" in app
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert ".evidence-drawer" in css
    assert ".evidence-backdrop" in css
    assert ".evidence-article-text" in css


def test_citation_title_article_suffix_and_fulltext_matching_are_exact():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function cleanCitationTitle(" in app
    assert "function citationTitleKey(" in app
    assert 'replace(/^中华人民共和国/, "")' in app
    article = app.split("function articleNoParts(", 1)[1].split(
        "function parseCiteText(", 1)[0]
    assert 'if (text === "全文")' in article
    assert "suffix" in article
    # 条号必须 fullmatch：第十九条之一与之二可区分，非法尾部和混写不解析。
    assert re.search(r"match\(/\^第\(\[.*?\]\+\)条", article)
    assert "(?:[（(][^（）()]+[）)])?" in article

    matched = app.split("function findCitationSingle(", 1)[1].split(
        "// 行内渲染", 1)[0]
    assert "citationTitleMatches(c, parsed.title, false)" in matched
    assert "const requested = articleNoParts(parsed.no);" in matched
    assert "if (!requested) return null;" in matched
    assert "candidate.fullText !== requested.fullText" in matched
    assert "candidate.value !== requested.value" in matched
    assert "candidate.suffix !== requested.suffix" in matched
    assert "indexOf" not in matched
    assert "byTitle" not in matched
    assert "citationTargetKey" in matched
    assert "return exact[0];" in matched


def test_chat_retry_serial_history_and_legacy_stop_have_safe_states():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "let chatRequests = [];" in app
    assert "function supersedeOtherChatRequests(" in app
    assert "function canRetryChatRequest(" in app
    can_retry = app.split("function canRetryChatRequest(", 1)[1].split(
        "async function retryChat(", 1)[0]
    assert "activeChatRequest" in can_retry
    assert "request.superseded" in can_retry
    assert "request.serial === chatRequestSerial" in can_retry
    assert "request.invalidated" not in can_retry

    retry = app.split("async function retryChat(", 1)[1].split(
        "function trimChatHistory(", 1)[0]
    assert "if (!canRetryChatRequest(request))" in retry
    assert "request.serial = ++chatRequestSerial;" in retry
    assert "request.invalidated = false;" in retry
    assert "request.superseded = false;" in retry
    send = app.split("async function sendChat(", 1)[1].split(
        "function clearChat(", 1)[0]
    assert "supersedeOtherChatRequests(null);" in send
    assert "serial: ++chatRequestSerial" in send

    assert "function trimChatHistory(" in app
    assert "slice(-80)" not in app
    assert 'chatHistory[start].role === "assistant"' in app
    assert app.count("trimChatHistory();") >= 2
    assert "function updateChatRetryButton(" in app
    update = app.split("function updateChatRetryButton(", 1)[1].split(
        "function renderChatFailure(", 1)[0]
    assert "activeChatRequest === request" in update
    assert "request.serial !== chatRequestSerial" in update
    assert "button.disabled" in update
    perform = app.split("async function performChatRequest(", 1)[1].split(
        "function stopChat(", 1)[0]
    assert "updateChatRetryButton(request);" in perform

    stop = app.split("function stopChat(", 1)[1].split(
        "function canRetryChatRequest(", 1)[0]
    legacy = stop.split("} else {", 1)[1]
    assert "request.invalidated = true;" in legacy
    assert "activeChatRequest = null;" not in legacy
    assert "setChatBusy(false);" not in legacy


def test_layered_dialog_escape_focus_and_reader_side_tab_contract():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    settings_handler = app.split("function handleSettingsKeydown(", 1)[1].split(
        "function settingsBody(", 1)[0]
    evidence_handler = app.split("function handleEvidenceKeydown(", 1)[1].split(
        "function renderEvidenceLaw(", 1)[0]
    reader_handler = app.split("function handleReaderSideKeydown(", 1)[1].split(
        "function readerRouteIsCurrent(", 1)[0]
    for handler in (settings_handler, evidence_handler, reader_handler):
        assert 'e.key === "Escape"' in handler
        assert "e.preventDefault();" in handler
        assert "e.stopImmediatePropagation();" in handler
    assert "settingsModal" in evidence_handler
    assert "settingsModal" in reader_handler and "evidenceDrawer" in reader_handler
    assert "function readerSideFocusable(" in app
    assert 'e.key !== "Tab"' in reader_handler
    assert "e.shiftKey" in reader_handler

    close_settings = app.split("function closeSettings(", 1)[1].split(
        "function settingsFocusable(", 1)[0]
    assert "evidenceIsOpen()" in close_settings
    assert 'readerSide").classList.contains("drawer-open")' in close_settings
    close_evidence = app.split("function closeEvidence(", 1)[1].split(
        "function evidenceFocusable(", 1)[0]
    assert "settingsModal" in close_evidence
    assert "readerSide" in close_evidence

    settings_bind = app.index('document.addEventListener("keydown", handleSettingsKeydown)')
    evidence_bind = app.index('document.addEventListener("keydown", handleEvidenceKeydown)')
    reader_bind = app.index('document.addEventListener("keydown", handleReaderSideKeydown)')
    assert settings_bind < evidence_bind < reader_bind


def test_evidence_reader_link_preserves_modified_click_native_behavior():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    reader = app.split('$("evidenceOpenReader").onclick', 1)[1].split("};", 1)[0]
    guard = "e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey"
    assert guard in reader
    assert reader.index(guard) < reader.index("e.preventDefault();")
    assert reader.index("closeEvidence(false);") < reader.index("navigate(hash);")


def test_citation_alternatives_and_title_variant_contract():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    parsed = app.split("function parseCiteText(", 1)[1].split(
        "function findCitationSingle(", 1)[0]
    assert "const alternatives = [];" in parsed
    assert "split(/[；;]/)" in parsed
    assert 'split("、")' in parsed
    assert "alternatives.push({ title: title, no: no })" in parsed
    assert "title: alternatives[0].title" in parsed
    assert "no: alternatives[0].no" in parsed
    assert "alternatives: alternatives" in parsed

    matched = app.split("function findCitation(", 1)[1].split(
        "// 行内渲染", 1)[0]
    assert "parsed.alternatives" in matched
    assert "findCitationSingle(citations, alternative)" in matched
    assert "if (hit) return hit;" in matched

    variants = app.split("function citationTitleVariants(", 1)[1].split(
        "function articleNoParts(", 1)[0]
    assert "citationTitleVariants(c.title, includeWithoutYear)" in variants
    assert "citationTitleVariants(c.ref_title, includeWithoutYear)" in variants
    assert "function citationTitleMatches(" in variants
    for alias in ("最高检", "最高人民检察院", "最高法", "最高人民法院", "证监会", "中国证券监督管理委员会"):
        assert alias in variants
    assert r"\d{4}年?(?:修订|修正)?" in variants
    assert "indexOf(b)" not in variants and "indexOf(a)" not in variants


def test_reader_side_desktop_breakpoint_closes_without_hidden_focus():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    handler = app.split("function handleReaderSideKeydown(", 1)[1].split(
        "function readerRouteIsCurrent(", 1)[0]
    assert "innerWidth > 1080" in handler
    close = app.split("function closeReaderSide(", 1)[1].split(
        "function openReaderSide(", 1)[0]
    assert "innerWidth <= 1080" in close
    assert "function handleReaderSideResize(" in app
    resize = app.split("function handleReaderSideResize(", 1)[1].split(
        "function readerRouteIsCurrent(", 1)[0]
    assert "innerWidth > 1080" in resize
    assert "closeReaderSide(false);" in resize
    assert 'window.addEventListener("resize", handleReaderSideResize);' in app


def test_chat_request_registry_is_bounded_without_breaking_retry():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function pruneChatRequests(" in app
    prune = app.split("function pruneChatRequests(", 1)[1].split(
        "function updateChatRetryButton(", 1)[0]
    assert "activeChatRequest === request" in prune
    assert "request.completed" in prune
    assert "!request.succeeded" in prune
    assert "!request.superseded" in prune
    assert "request.serial === chatRequestSerial" in prune
    supersede = app.split("function supersedeOtherChatRequests(", 1)[1].split(
        "function renderChatFailure(", 1)[0]
    assert "pruneChatRequests();" in supersede
    perform = app.split("async function performChatRequest(", 1)[1].split(
        "function stopChat(", 1)[0]
    assert "request.completed = false;" in perform
    assert "request.succeeded = false;" in perform
    assert "request.succeeded = true;" in perform
    assert "request.completed = true;" in perform
    assert "pruneChatRequests();" in perform
    send = app.split("async function sendChat(", 1)[1].split(
        "function clearChat(", 1)[0]
    assert "completed: false" in send
    assert "succeeded: false" in send


def test_citation_year_priority_and_scope_reset_contract():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    single = app.split("function findCitationSingle(", 1)[1].split(
        "function findCitation(", 1)[0]
    assert "const fullCandidates" in single
    assert "citationTitleMatches(c, parsed.title, false)" in single
    assert "!citationTitleHasYear(parsed.title) && fullCandidates.length === 0" in single
    assert "citationTitleMatches(c, parsed.title, true)" in single
    assert "const exact = titleCandidates.filter" in single
    assert "function citationTitleHasYear(" in app
    assert "function citationTargetKey(" in app
    assert "if (citationTargetKey(c) !== target) return null;" in single
    variants = app.split("function citationTitleVariants(", 1)[1].split(
        "function citationTitleMatches(", 1)[0]
    assert "includeWithoutYear" in variants
    clean = app.split("function cleanCitationTitle(", 1)[1].split(
        "function citationTitleKey(", 1)[0]
    assert "outerEnd" in clean


def test_chat_scope_switch_resets_history_requests_and_active_safely():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "let chatScopeKey = \"all\";" in app
    assert "function resetChatForScope(" in app
    reset = app.split("function resetChatForScope(", 1)[1].split(
        "function updateChatScope(", 1)[0]
    assert "if (nextKey === chatScopeKey) return;" in reset
    assert "stopChat();" in reset
    assert "request.invalidated = true;" in reset
    assert "request.superseded = true;" in reset
    assert "chatRequestSerial++;" in reset
    assert "chatRequests = [];" in reset
    assert "chatHistory = [];" in reset
    assert "已切换研究范围，开始新对话" in reset
    update = app.split("function updateChatScope(", 1)[1].split(
        "function setChatBusy(", 1)[0]
    assert '"law:" + currentLawId' in update
    assert "resetChatForScope(nextKey);" in update
    assert '$("chatScope").onchange' in app
    ask = app.split('$("btnAskLaw").onclick', 1)[1].split("};", 1)[0]
    assert '$("chatScope").value = "current";' in ask
    assert "updateChatScope(parseRoute());" in ask


def test_stream_reader_cancellation_and_perform_abort_contract():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    reader = app.split("async function readChatStream(", 1)[1].split(
        "async function requestChat(", 1)[0]
    assert "await reader.cancel();" in reader
    assert "!error.chatTerminal" in reader
    assert "throw error;" in reader
    assert "error.chatTerminal = true;" in reader
    perform = app.split("async function performChatRequest(", 1)[1].split(
        "function stopChat(", 1)[0]
    catch = perform.split("} catch (e) {", 1)[1].split("} finally {", 1)[0]
    assert "chatRequestIsCurrent(request)" in catch
    assert "request.controller.abort();" in catch
    assert perform.index("activeChatRequest = null;") < perform.index(
        "updateChatRetryButton(request);")


def test_citation_only_strips_outer_book_title_marks():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    clean = app.split("function cleanCitationTitle(", 1)[1].split(
        "function citationTitleKey(", 1)[0]
    assert "title.match(/^《([^》]+)》$/)" in clean
    assert "title.match(/《([^》]+)》/)" not in clean
    assert "book.index" not in clean
    assert "title = book[1]" not in clean
    assert "关于修改" not in clean


def test_all_enter_handlers_ignore_ime_before_default_or_actions():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    handlers = {
        "global": app.split('$("globalSearch").addEventListener("keydown"', 1)[1].split("});", 1)[0],
        "home": app.split('$("homeSearch").addEventListener("keydown"', 1)[1].split("});", 1)[0],
        "search_page": app.split('$("searchPageInput").addEventListener("keydown"', 1)[1].split("});", 1)[0],
        "article": app.split('$("articleSearchInput").addEventListener("keydown"', 1)[1].split("});", 1)[0],
        "chat": app.split('$("chatInput").addEventListener("keydown"', 1)[1].split("});", 1)[0],
    }
    for name, handler in handlers.items():
        assert "e.isComposing || e.keyCode === 229" in handler, name
        guard = handler.index("e.isComposing || e.keyCode === 229")
        assert "e.preventDefault();" in handler, name
        assert guard < handler.index("e.preventDefault();"), name
        for action in ("submitGlobalSearch", "navigate(", "submitSearchPage", "searchLawArticles", "sendChat"):
            if action in handler:
                assert guard < handler.index(action), (name, action)


def test_empty_query_with_filters_loads_laws_without_empty_article_request():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    unified = app.split("async function loadUnifiedSearch(", 1)[1].split(
        "// 纯文本扫描", 1)[0]
    assert "const hasFilters = Object.keys(searchFilters(route)).length > 0;" in unified
    assert 'if (!q && !hasFilters && route.scope !== "law")' in unified
    articles = app.split("async function loadSearchArticles(", 1)[1].split(
        "async function loadUnifiedSearch(", 1)[0]
    assert articles.index("if (!q)") < articles.index("API.search(")
    assert "法条搜索不支持空查询" in articles


def test_route_focus_does_not_steal_dialog_chat_or_input_focus():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function shouldFocusMainAfterRoute()" in app
    guard = app.split("function shouldFocusMainAfterRoute()", 1)[1].split(
        "\n}", 1)[0]
    assert '$("settingsModal").classList.contains("hidden")' in guard
    assert "evidenceIsOpen()" in guard
    assert "chatPanelOpen()" in guard and '$("chatPanel").contains(active)' in guard
    assert 'active.matches("input, textarea, select, [contenteditable=' in guard
    route = app.split("async function handleRoute() {", 1)[1].split(
        "function ensureStats(", 1)[0]
    assert "if (!shouldFocusMainAfterRoute()) return;" in route


def test_wiki_active_request_tracks_law_across_pagination():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "const activeWikiRequests = {};" in app
    assert "function showWikiGenerating(" in app
    detail = app.split("async function loadLawDetail(", 1)[1].split(
        "function articleSnippet(", 1)[0]
    assert "activeWikiRequests[id]" in detail
    assert "showWikiGenerating(id);" in detail
    generate = app.split("async function generateWiki(", 1)[1].split(
        "// ---------- 配置弹窗", 1)[0]
    assert "activeWikiRequests[id]" in generate
    assert "delete activeWikiRequests[id]" in generate
    assert "expectedHash" not in generate
    assert "readerRouteIsCurrent" not in generate
    assert 'route.name === "law" && route.id === id' in generate


def test_settings_slow_response_cannot_reopen_closed_modal():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "let settingsSerial = 0;" in app
    assert "let settingsOpening = false;" in app
    opened = app.split("async function openSettings()", 1)[1].split(
        "function closeSettings()", 1)[0]
    assert "if (settingsOpening" in opened
    assert "const ticket = ++settingsSerial;" in opened
    assert "ticket !== settingsSerial" in opened
    assert '$("settingsModal").classList.contains("hidden")' in opened
    closed = app.split("function closeSettings()", 1)[1].split(
        "function settingsFocusable()", 1)[0]
    assert "settingsSerial++;" in closed
    assert "settingsOpening = false;" in closed


def test_reader_side_dialog_aria_is_scoped_to_open_narrow_drawer():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    opened = app.split("function openReaderSide()", 1)[1].split(
        "function readerSideFocusable()", 1)[0]
    assert 'setAttribute("role", "dialog")' in opened
    assert 'setAttribute("aria-modal", "true")' in opened
    assert 'setAttribute("aria-label", "法规解读与关联")' in opened
    closed = app.split("function closeReaderSide(", 1)[1].split(
        "function openReaderSide()", 1)[0]
    for attribute in ("role", "aria-modal", "aria-label"):
        assert f'removeAttribute("{attribute}")' in closed


def test_dead_frontend_helpers_are_not_kept_for_static_contracts():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function btn(" not in app
    assert "function articleNoValue(" not in app
    assert "function escReg(" not in app
    assert "law: (id)" not in app
    assert "function articleNoParts(" in app
