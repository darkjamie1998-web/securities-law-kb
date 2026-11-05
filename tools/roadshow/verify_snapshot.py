"""用本机 Chrome 对完全离线的单文件快照做真实浏览器验收。

默认执行完整 41 项验收（5 路由 + 11 基础交互 + 25 深度/移动端）。
--routes-only 仅执行 5 项路由冒烟；--interactions 保留 16 项旧模式，--deep 同默认全量。
被测页面始终以 file:// 打开，browser context 固定 offline=True，不启动业务服务器。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT = ROOT / "证券法律法规知识库-前端快照.html"
DEFAULT_CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
FIXTURE = json.loads(
    (ROOT / "tools" / "roadshow" / "fixture.json").read_text(encoding="utf-8")
)

ROUTES = {
    "#/home": "证券法律法规研究",
    "#/laws": "全部法规",
    "#/search?q=减持计划": "减持计划",
    "#/law/216": "上市公司信息披露管理办法",
    "#/graph": "知识图谱",
}

ROUTE_VIEWS = {
    "#/home": "#viewHome",
    "#/laws": "#viewList",
    "#/search?q=减持计划": "#viewSearch",
    "#/law/216": "#viewDetail",
    "#/graph": "#viewGraph",
}


@dataclass
class Diagnostics:
    """保留本次页面生命周期内的全部错误与外网请求。"""

    page_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    http_requests: list[str] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: self.console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on(
            "request",
            lambda request: self.http_requests.append(
                f"{request.method} {request.url}"
            )
            if request.url.startswith(("http://", "https://"))
            else None,
        )

    def print_summary(self) -> None:
        print("\n浏览器诊断")
        print(f"  pageerror: {len(self.page_errors)}")
        for item in self.page_errors:
            print(f"    {item}")
        print(f"  console error: {len(self.console_errors)}")
        for item in self.console_errors:
            print(f"    {item}")
        print(f"  HTTP/HTTPS request: {len(self.http_requests)}")
        for item in self.http_requests:
            print(f"    {item}")

    def errors(self) -> list[str]:
        return (
            [f"存在 pageerror：{item}" for item in self.page_errors]
            + [f"存在 console error：{item}" for item in self.console_errors]
            + [f"存在外部请求：{item}" for item in self.http_requests]
        )


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""


def compact_error(error: Exception) -> str:
    """保留 Playwright 最有用的首行断言信息，避免淹没其他检查结果。"""

    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return " | ".join(lines[:3]) or error.__class__.__name__


def run_check(name: str, check: Callable[[], None]) -> CheckResult:
    try:
        check()
    except Exception as error:  # 单项失败后继续，完整暴露旧快照缺失的能力。
        result = CheckResult(name, False, compact_error(error))
        print(f"FAIL {name}：{result.reason}")
        return result
    print(f"PASS {name}")
    return CheckResult(name, True)


def assert_hash(page: Page, expected: str) -> None:
    """按解码后的 hash 断言，兼容浏览器对中文查询参数的百分号编码。"""

    page.wait_for_function(
        """expected => {
          try { return decodeURIComponent(location.hash) === expected; }
          catch (_) { return location.hash === expected; }
        }""",
        arg=expected,
    )


def assert_hash_pattern(page: Page, pattern: str) -> None:
    page.wait_for_function(
        """pattern => {
          const hash = (() => {
            try { return decodeURIComponent(location.hash); }
            catch (_) { return location.hash; }
          })();
          return new RegExp(pattern).test(hash);
        }""",
        arg=pattern,
    )


def assert_no_horizontal_overflow(page: Page, label: str) -> None:
    """关键页面和浮层打开状态都不得出现页面级横向溢出。"""

    metrics = page.evaluate(
        """() => ({
          innerWidth: window.innerWidth,
          scrollWidth: document.documentElement.scrollWidth
        })"""
    )
    assert metrics["scrollWidth"] <= metrics["innerWidth"], (
        f"{label} 横向溢出：scrollWidth={metrics['scrollWidth']} "
        f"> innerWidth={metrics['innerWidth']}"
    )


def expect_view_text(page: Page, view_selector: str, text: str) -> None:
    """文本必须位于目标视图且实际可见，不能由隐藏视图或侧栏同名文字冒充。"""

    expect(page.locator(view_selector)).to_be_visible()
    expect(
        page.locator(view_selector).get_by_text(text, exact=False).first
    ).to_be_visible()
    assert_no_horizontal_overflow(page, f"{view_selector}（{text}）")


def navigate_hash(
    page: Page,
    route: str,
    expected_text: Optional[str] = None,
    view_selector: Optional[str] = None,
) -> None:
    """通过 location.hash 导航，并等待路由及（可选的）具体视图内容。"""

    page.evaluate("route => { location.hash = route; }", route)
    assert_hash(page, route)
    if expected_text is not None:
        if view_selector is None:
            view_selector = ROUTE_VIEWS.get(route)
            if view_selector is None and route.startswith("#/law/"):
                view_selector = "#viewDetail"
            if view_selector is None and route.startswith("#/search"):
                view_selector = "#viewSearch"
        expect_view_text(page, view_selector, expected_text)


def open_home_from_brand(page: Page) -> None:
    page.locator("#brand").click()
    assert_hash(page, "#/home")
    expect_view_text(page, "#viewHome", "证券法律法规研究")


def open_laws_from_sidebar(page: Page) -> None:
    page.locator("#navLaws").click()
    assert_hash(page, "#/laws")
    expect_view_text(page, "#viewList", "全部法规")
    expect(page.locator("#lawList .law-card").first).to_be_visible()


def open_graph_from_sidebar(page: Page) -> None:
    page.locator("#navGraph").click()
    assert_hash(page, "#/graph")
    expect_view_text(page, "#viewGraph", "知识图谱")
    expect(page.locator("#graphCanvas")).to_be_visible()
    assert_no_horizontal_overflow(page, "桌面知识图谱")


def search_reduction_plan(page: Page) -> None:
    search = page.locator("#globalSearch")
    expect(search).to_be_editable()
    search.fill("减持计划")
    page.locator("#btnGlobalSearch").click()
    assert_hash(page, "#/search?q=减持计划")
    expect_view_text(page, "#viewSearch", "减持计划")
    expect(page.locator("#searchArticleResults .search-hit").first).to_be_visible()
    assert_no_horizontal_overflow(page, "桌面减持搜索结果")


def click_first_search_article(page: Page) -> None:
    navigate_hash(page, "#/search?q=减持计划", "减持计划")
    first = page.locator("#searchArticleResults .search-hit").first
    expect(first).to_be_visible()
    first.click()
    assert_hash_pattern(page, r"^#/law/[1-9][0-9]*(?:\?|$)")
    expect(page.locator("#viewDetail")).to_be_visible()
    expect(page.locator("#lawHeader h2")).to_be_visible()
    expect(page.locator("#articleList .reader-article").first).to_be_visible()
    assert_no_horizontal_overflow(page, "桌面法规阅读器")


def locate_reader_article(page: Page, article_no: str = "第一条") -> None:
    navigate_hash(page, "#/law/216", "上市公司信息披露管理办法")
    toc_link = page.locator("#articleToc a").filter(
        has_text=re.compile(f"^{re.escape(article_no)}$")
    )
    expect(toc_link).to_be_visible()
    toc_link.click()
    assert_hash_pattern(page, rf"^#/law/216\?.*article_no={re.escape(article_no)}")
    expect(toc_link).to_have_attribute("aria-current", "location")
    article = page.locator("#articleList .reader-article").filter(
        has=page.locator(
            ".no", has_text=re.compile(f"^{re.escape(article_no)}$")
        )
    )
    expect(article).to_be_visible()
    expect(article).to_be_in_viewport()


def search_within_law(page: Page, query: str = "信息披露") -> None:
    navigate_hash(page, "#/law/216", "上市公司信息披露管理办法")
    search = page.locator("#articleSearchInput")
    expect(search).to_be_editable()
    search.fill(query)
    page.locator("#btnArticleSearch").click()
    expect(page.locator("#articleSearchStatus")).to_contain_text("条命中")
    first = page.locator("#articleSearchResults .reader-search-hit").first
    expect(first).to_be_visible()
    expect(first).to_contain_text(query)


def open_relations(page: Page) -> None:
    navigate_hash(page, "#/law/216", "上市公司信息披露管理办法")
    page.locator("#tabRelations").click()
    expect(page.locator("#tabRelations")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.locator("#relationPanel")).to_be_visible()
    expect(page.locator("#relationList .relation-item a").first).to_be_visible()


def open_chat(page: Page) -> None:
    page.locator("#chatFab").click()
    expect(page.locator("#chatPanel")).to_be_visible()
    expect(page.locator("#chatPanelHead")).to_contain_text("对话研究")
    expect(page.locator("#chatInput")).to_be_visible()
    assert_no_horizontal_overflow(page, "桌面聊天面板")


def open_settings(page: Page) -> None:
    page.locator("#btnSettings").click()
    expect(page.locator("#settingsModal")).to_be_visible()
    expect(page.locator("#settingsTitle")).to_contain_text("大模型配置")
    page.locator("#btnCloseCfg").click()
    expect(page.locator("#settingsModal")).to_be_hidden()


def verify_history_back_forward(page: Page) -> None:
    navigate_hash(page, "#/home", "证券法律法规研究")
    navigate_hash(page, "#/laws", "全部法规")
    page.evaluate("() => history.back()")
    assert_hash(page, "#/home")
    expect_view_text(page, "#viewHome", "证券法律法规研究")
    page.evaluate("() => history.forward()")
    assert_hash(page, "#/laws")
    expect_view_text(page, "#viewList", "全部法规")


def run_route_checks(page: Page) -> list[CheckResult]:
    results = []
    for route, text in ROUTES.items():
        results.append(
            run_check(
                f"路由 {route} -> {text}",
                lambda route=route, text=text: navigate_hash(
                    page, route, text, ROUTE_VIEWS[route]
                ),
            )
        )
    return results


def run_interaction_checks(page: Page) -> list[CheckResult]:
    """独立命名的路演步骤；后续任务可复用函数或扩展参数。"""

    checks = [
        ("交互 品牌回首页", open_home_from_brand),
        ("交互 侧栏全部法规", open_laws_from_sidebar),
        ("交互 侧栏知识图谱", open_graph_from_sidebar),
        ("交互 搜索减持计划", search_reduction_plan),
        ("交互 点击首个法条", click_first_search_article),
        ("交互 阅读器条号定位", locate_reader_article),
        ("交互 篇内搜索", search_within_law),
        ("交互 打开关联", open_relations),
        ("交互 打开聊天", open_chat),
        ("交互 打开设置", open_settings),
        ("交互 前进后退", verify_history_back_forward),
    ]
    return [run_check(name, lambda check=check: check(page)) for name, check in checks]


def normalized_text(value: str) -> str:
    return " ".join(value.split())


def ensure_chat_open(page: Page) -> None:
    if not page.locator("#chatPanel").is_visible():
        page.locator("#chatFab").click()
    expect(page.locator("#chatPanel")).to_be_visible()
    expect(page.locator("#chatInput")).to_be_editable()
    assert_no_horizontal_overflow(page, "聊天面板")


def send_chat(page: Page, question: str):
    """发送一条真实 UI 消息，并等待最终回答或终局错误取代全部阶段。"""

    ensure_chat_open(page)
    previous = page.locator("#chatLog .msg.ai").count()
    page.locator("#chatInput").fill(question)
    page.locator("#btnSend").click()
    answer = page.locator("#chatLog .msg.ai").nth(previous)
    expect(answer.locator(".chat-answer, .chat-error").first).to_be_visible()
    expect(answer.locator(".chat-progress")).to_have_count(0)
    expect(page.locator("#btnSend")).to_be_enabled()
    expect(page.locator("#btnStopChat")).to_be_disabled()
    return answer


def assert_preset_answer(answer, preset: dict) -> None:
    expect(answer.locator(".chat-answer")).to_be_visible()
    actual = normalized_text(answer.locator(".chat-answer").inner_text())
    for paragraph in preset["content"].split("\n\n"):
        expected = normalized_text(paragraph)
        assert expected in actual, "回答正文未完整呈现 fixture 的预置段落"
    expect(answer.locator(".searches")).to_have_text(
        "已检索：" + "；".join(preset["searches"])
    )
    references = answer.locator(".cite-reference")
    expect(references).to_have_count(len(preset["citations"]))
    for index, citation in enumerate(preset["citations"]):
        reference = references.nth(index)
        expect(reference).to_contain_text(citation["title"])
        expect(reference).to_contain_text(citation["article_no"])
        expect(reference).to_have_attribute("data-citation", str(index))


def verify_citation_evidence_and_reader(page: Page, answer, index: int, citation: dict) -> None:
    """引用 → 真实证据正文 → 阅读器目标条文，三层 ID 与条号必须一致。"""

    article = FIXTURE["article_by_id"][str(citation["article_id"])]
    law = FIXTURE["laws"][str(citation["law_id"])]
    answer.locator(f'.cite-reference[data-citation="{index}"]').click()
    expect(page.locator("#evidenceDrawer")).to_be_visible()
    expect(page.locator("#evidenceBody .evidence-law h3")).to_have_text(law["title"])
    evidence = page.locator("#evidenceBody .evidence-article")
    expect(evidence.locator("h3")).to_have_text(citation["article_no"])
    expect(evidence.locator(".evidence-article-text")).to_have_text(article["text"])
    assert_no_horizontal_overflow(page, "桌面证据抽屉")

    open_reader = page.locator("#evidenceOpenReader")
    expect(open_reader).to_be_visible()
    href = open_reader.get_attribute("href") or ""
    assert href.startswith(f'#/law/{citation["law_id"]}?'), href
    assert f'law/{citation["law_id"]}' in href
    open_reader.click()
    expected_hash = f'#/law/{citation["law_id"]}?article_no={citation["article_no"]}'
    assert_hash(page, expected_hash)
    expect(page.locator("#lawHeader h2")).to_have_text(law["title"])
    target = page.locator(f'#art-{citation["article_id"]}')
    expect(target).to_be_visible()
    expect(target).to_be_in_viewport()
    expect(target.locator(".no")).to_have_text(citation["article_no"])
    expect(target.locator(".reader-article-text")).to_have_text(article["text"])
    toc_target = page.locator("#articleToc a[aria-current='location']")
    expect(toc_target).to_have_text(citation["article_no"])


def verify_reduction_chat_evidence(page: Page) -> None:
    navigate_hash(page, "#/home", "证券法律法规研究")
    preset = FIXTURE["chats"]["reduction"]
    answer = send_chat(page, preset["question"])
    assert_preset_answer(answer, preset)
    assert "十五个交易日" in answer.inner_text()
    verify_citation_evidence_and_reader(page, answer, 0, preset["citations"][0])


def verify_disclosure_chat_evidence(page: Page) -> None:
    navigate_hash(page, "#/home", "证券法律法规研究")
    preset = FIXTURE["chats"]["disclosure"]
    answer = send_chat(page, preset["question"])
    assert_preset_answer(answer, preset)
    assert "商业秘密" in answer.inner_text()
    for index, citation in enumerate(preset["citations"]):
        verify_citation_evidence_and_reader(page, answer, index, citation)


def verify_unknown_chat_and_retry(page: Page) -> None:
    navigate_hash(page, "#/home", "证券法律法规研究")
    # 只统计真实发送给适配器的流式接口调用，防止旧错误 DOM 让“重试”空转也通过。
    page.evaluate(
        """() => {
          const stats = { streamRequests: 0 };
          window.__chatRetryRequestStats = stats;
          const originalFetch = window.fetch;
          window.fetch = function (input, init) {
            const rawUrl = typeof input === 'string' ? input :
              String(input && input.url || input || '');
            try {
              const url = new URL(rawUrl, 'http://offline.local');
              if (url.pathname === '/api/chat/stream') stats.streamRequests += 1;
            } catch (_) { /* 非 URL 输入交给原 fetch 处理，不计为聊天请求。 */ }
            return originalFetch.call(this, input, init);
          };
        }"""
    )
    before_initial = page.evaluate(
        "() => window.__chatRetryRequestStats.streamRequests"
    )
    answer = send_chat(page, "减持个税如何计算？")
    assert page.evaluate(
        "() => window.__chatRetryRequestStats.streamRequests"
    ) == before_initial + 1
    error = answer.locator(".chat-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("离线演示仅支持预置问题")
    expect(error).to_contain_text(FIXTURE["chats"]["reduction"]["question"])
    expect(error).to_contain_text(FIXTURE["chats"]["disclosure"]["question"])
    expect(answer.locator(".chat-answer")).to_have_count(0)
    expect(answer.locator(".cite-reference")).to_have_count(0)

    first_error = error.element_handle()
    assert first_error is not None
    before_retry = page.evaluate(
        "() => window.__chatRetryRequestStats.streamRequests"
    )
    retry = answer.locator(".chat-retry")
    expect(retry).to_be_enabled()
    retry.click()
    page.wait_for_function(
        "before => window.__chatRetryRequestStats.streamRequests === before + 1",
        arg=before_retry,
    )
    # 首个错误必须离开 DOM，随后出现新的终局错误；不能复用点击前的文本断言。
    page.wait_for_function("element => !element.isConnected", arg=first_error)
    second_error = answer.locator(".chat-error")
    expect(second_error).to_be_visible()
    expect(second_error).to_contain_text("离线演示仅支持预置问题")
    expect(second_error).to_contain_text(FIXTURE["chats"]["reduction"]["question"])
    expect(second_error).to_contain_text(FIXTURE["chats"]["disclosure"]["question"])
    expect(answer.locator(".chat-answer")).to_have_count(0)
    expect(answer.locator(".cite-reference")).to_have_count(0)
    expect(page.locator("#btnSend")).to_be_enabled()
    assert page.evaluate(
        "() => window.__chatRetryRequestStats.streamRequests"
    ) == before_retry + 1


def verify_current_law_scope_rejection(page: Page) -> None:
    navigate_hash(page, "#/law/293")
    ensure_chat_open(page)
    page.locator("#btnAskLaw").click()
    expect(page.locator("#chatScope")).to_have_value("current")
    answer = send_chat(page, FIXTURE["chats"]["reduction"]["question"])
    error = answer.locator(".chat-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("当前法规未预置该问题")
    expect(error).to_contain_text("离线演示仅支持预置问题")
    expect(answer.locator(".chat-answer")).to_have_count(0)
    expect(answer.locator(".cite-reference")).to_have_count(0)


def verify_nonstream_unknown_chat_error(page: Page) -> None:
    navigate_hash(page, "#/home", "证券法律法规研究")
    page.evaluate(
        """() => {
          window.__verifyOriginalReadableStream = window.ReadableStream;
          window.ReadableStream = undefined;
        }"""
    )
    try:
        answer = send_chat(page, "减持个税如何计算？")
        error = answer.locator(".chat-error")
        expect(error).to_be_visible()
        expect(error).to_contain_text("离线演示仅支持预置问题")
        expect(error).to_contain_text(FIXTURE["chats"]["reduction"]["question"])
        expect(error).to_contain_text(FIXTURE["chats"]["disclosure"]["question"])
        expect(error).not_to_contain_text("格式不完整")
        expect(answer.locator(".chat-answer")).to_have_count(0)
        expect(answer.locator(".cite-reference")).to_have_count(0)
        expect(page.locator("#btnSend")).to_be_enabled()
    finally:
        page.evaluate(
            "() => { window.ReadableStream = window.__verifyOriginalReadableStream; }"
        )


def verify_chat_panel_controls(page: Page) -> None:
    ensure_chat_open(page)
    page.locator("#btnMinChat").click()
    expect(page.locator("#chatPanel")).to_be_hidden()
    page.locator("#chatFab").click()
    expect(page.locator("#chatPanel")).to_be_visible()

    panel = page.locator("#chatPanel")
    before = panel.bounding_box()
    assert before is not None
    head = page.locator("#chatPanelHead").bounding_box()
    assert head is not None
    start_x, start_y = head["x"] + 75, head["y"] + head["height"] / 2
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(start_x - 90, start_y - 45, steps=5)
    page.mouse.up()
    moved = panel.bounding_box()
    assert moved is not None
    assert abs(moved["x"] - before["x"]) > 40
    assert abs(moved["y"] - before["y"]) > 20
    assert page.evaluate("() => !!localStorage.getItem('chatPanelPos')")

    resized_before = panel.bounding_box()
    handle = page.locator("#chatResize").bounding_box()
    assert resized_before is not None and handle is not None
    resize_x = handle["x"] + handle["width"] / 2
    resize_y = handle["y"] + handle["height"] / 2
    page.mouse.move(resize_x, resize_y)
    page.mouse.down()
    page.mouse.move(resize_x + 45, resize_y + 35, steps=5)
    page.mouse.up()
    resized = panel.bounding_box()
    assert resized is not None
    assert resized["width"] > resized_before["width"] + 20
    assert resized["height"] > resized_before["height"] + 15
    assert page.evaluate("() => !!localStorage.getItem('chatPanelSize')")

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#btnClearChat").click()
    expect(page.locator("#chatLog .chat-hint")).to_have_text("对话已清空。")
    expect(page.locator("#chatLog .msg")).to_have_count(0)


def verify_law_catalog_two_pages(page: Page) -> None:
    navigate_hash(page, "#/laws", "全部法规")
    expect(page.locator("#listCount")).to_have_text("共 8 部")
    expect(page.locator("#lawList .law-card")).to_have_count(6)
    expect(page.locator("#pager .page-info")).to_have_text("第 1 / 2 页")
    first_page = page.locator("#lawList .law-card").all_inner_texts()
    page.locator("#pager a.page-link").filter(has_text="下一页").click()
    assert_hash_pattern(page, r"^#/laws\?.*page=2")
    expect(page.locator("#lawList .law-card")).to_have_count(2)
    expect(page.locator("#pager .page-info")).to_have_text("第 2 / 2 页")
    second_page = page.locator("#lawList .law-card").all_inner_texts()
    assert first_page != second_page
    assert "中华人民共和国证券法" in "\n".join(second_page)
    page.locator("#pager a.page-link").filter(has_text="上一页").click()
    expect(page.locator("#lawList .law-card")).to_have_count(6)


def search_all_securities(page: Page) -> None:
    navigate_hash(page, "#/home", "证券法律法规研究")
    page.locator("#globalSearch").fill("证券")
    page.locator("#btnGlobalSearch").click()
    assert_hash(page, "#/search?q=证券")
    expect(page.locator("#searchLawResults .law-card")).to_have_count(6)
    expect(page.locator("#searchArticleResults .search-hit")).to_have_count(10)


def verify_search_scopes(page: Page) -> None:
    search_all_securities(page)
    tabs = page.locator("#searchTabs [data-scope]")
    tabs.filter(has_text="法条").click()
    assert_hash_pattern(page, r"^#/search\?.*scope=article")
    expect(page.locator("#lawSearchPanel")).to_be_hidden()
    expect(page.locator("#articleSearchPanel")).to_be_visible()
    expect(page.locator("#searchArticleResults .search-hit")).to_have_count(10)
    expect(page.locator("#articleSearchCount")).to_contain_text("共164条候选法条")

    tabs.filter(has_text="法规").click()
    assert_hash_pattern(page, r"^#/search\?.*scope=law")
    expect(page.locator("#lawSearchPanel")).to_be_visible()
    expect(page.locator("#articleSearchPanel")).to_be_hidden()
    expect(page.locator("#searchLawResults .law-card")).to_have_count(6)
    expect(page.locator("#lawSearchCount")).to_have_text("共8部法规")

    tabs.filter(has_text="全部").click()
    expect(page.locator("#lawSearchPanel")).to_be_visible()
    expect(page.locator("#articleSearchPanel")).to_be_visible()
    expect(page.locator("#searchCount")).to_have_text("共8部法规 · 共164条候选法条")


def verify_search_filters(page: Page) -> None:
    search_all_securities(page)
    page.locator("#filterLevel").select_option("法律")
    assert_hash_pattern(page, r"^#/search\?.*level=法律")
    expect(page.locator("#searchLawResults .law-card")).to_have_count(1)
    expect(page.locator("#searchLawResults .law-card")).to_contain_text("中华人民共和国证券法")
    expect(page.locator("#lawSearchCount")).to_have_text("共1部法规")
    expect(page.locator("#searchArticleResults .search-hit").first).to_contain_text(
        "中华人民共和国证券法"
    )
    page.locator("#btnClearFilters").click()
    assert_hash(page, "#/search?q=证券")
    expect(page.locator("#searchLawResults .law-card")).to_have_count(6)
    expect(page.locator("#lawSearchCount")).to_have_text("共8部法规")


def verify_independent_search_pagination(page: Page) -> None:
    search_all_securities(page)
    first_article = page.locator("#searchArticleResults .search-hit").first.get_attribute("href")
    assert first_article
    page.locator("#searchLawPager a.page-link").filter(has_text="下一页").click()
    assert_hash_pattern(page, r"^#/search\?.*law_page=2")
    expect(page.locator("#searchLawResults .law-card")).to_have_count(2)
    expect(page.locator("#searchArticleResults .search-hit")).to_have_count(10)
    expect(page.locator("#searchArticleResults .search-hit").first).to_have_attribute(
        "href", first_article
    )
    expect(page.locator("#searchLawPager .page-info")).to_have_text("第 2 / 2 页")
    expect(page.locator("#searchArticlePager .page-info")).to_have_text("第 1 / 17 页")

    law_page_two_hrefs = page.locator("#searchLawResults .law-card").evaluate_all(
        "elements => elements.map(element => element.getAttribute('href'))"
    )
    page.locator("#searchArticlePager a.page-link").filter(has_text="下一页").click()
    assert_hash_pattern(page, r"^#/search\?.*law_page=2.*article_page=2")
    expect(page.locator("#searchArticleResults .search-hit")).to_have_count(10)
    expect(page.locator("#searchArticlePager .page-info")).to_have_text("第 2 / 17 页")
    assert page.locator("#searchLawResults .law-card").evaluate_all(
        "elements => elements.map(element => element.getAttribute('href'))"
    ) == law_page_two_hrefs
    assert page.locator(
        "#searchArticleResults .search-hit"
    ).first.get_attribute("href") != first_article

    page.locator("#searchLawPager a.page-link").filter(has_text="上一页").click()
    assert_hash_pattern(page, r"^#/search\?.*article_page=2")
    expect(page.locator("#searchLawResults .law-card")).to_have_count(6)
    expect(page.locator("#searchArticlePager .page-info")).to_have_text("第 2 / 17 页")


def verify_empty_search(page: Page) -> None:
    navigate_hash(page, "#/home", "证券法律法规研究")
    page.locator("#globalSearch").fill("")
    page.locator("#btnGlobalSearch").click()
    assert_hash(page, "#/search")
    expect(page.locator("#searchLawResults .empty-box")).to_contain_text("输入关键词后")
    expect(page.locator("#searchArticleResults .empty-box")).to_contain_text("可尝试")
    expect(page.locator("#searchLawResults .law-card")).to_have_count(0)
    expect(page.locator("#searchArticleResults .search-hit")).to_have_count(0)


def verify_no_result_search(page: Page) -> None:
    navigate_hash(page, "#/search?q=绝不存在的关键词", "绝不存在的关键词")
    expect(page.locator("#searchLawResults .empty-box")).to_contain_text("未找到匹配法规")
    expect(page.locator("#searchArticleResults .empty-box")).to_contain_text("未找到候选法条")
    expect(page.locator("#searchCount")).to_have_text("共0部法规 · 共0条候选法条")
    expect(page.locator(".error-box")).to_have_count(0)


def verify_missing_law_state(page: Page) -> None:
    navigate_hash(page, "#/law/999999")
    expect(page.locator("#viewDetail")).to_be_visible()
    expect(page.locator("#lawHeader h2")).to_have_text("加载失败")
    expect(page.locator("#lawHeader .error-box")).to_have_text("法规不存在")
    expect(page.locator("#articleList .error-box")).to_contain_text("正文加载失败")
    expect(page.locator("#articleList .reader-article")).to_have_count(0)


def verify_securities_law_reader_pages(page: Page) -> None:
    navigate_hash(page, "#/law/20", "中华人民共和国证券法")
    expect(page.locator("#articleList .reader-article")).to_have_count(50)
    expect(page.locator("#articlePageMeta")).to_have_text("共 60 条 · 每页 50 条")
    expect(page.locator("#articlePageInfo")).to_have_text("第 1 / 2 页")
    expect(page.locator("#articleList .reader-article .no").first).to_have_text("第一条")
    expect(page.locator("#articleList .reader-article .no").last).to_have_text("第五十条")
    page.locator("#btnArticleNext").click()
    assert_hash(page, "#/law/20?page=2")
    expect(page.locator("#articleList .reader-article")).to_have_count(10)
    expect(page.locator("#articleList .reader-article .no").first).to_have_text("第五十一条")
    expect(page.locator("#articleList .reader-article .no").last).to_have_text("第六十条")
    expect(page.locator("#articlePageInfo")).to_have_text("第 2 / 2 页")
    expect(page.locator("#btnArticleNext")).to_be_disabled()
    page.locator("#btnArticlePrev").click()
    expect(page.locator("#articleList .reader-article")).to_have_count(50)


def verify_securities_law_toc_target(page: Page) -> None:
    navigate_hash(page, "#/law/20", "中华人民共和国证券法")
    link = page.locator("#articleToc a").filter(has_text=re.compile("^第五十一条$"))
    link.click()
    assert_hash(page, "#/law/20?article_no=第五十一条")
    expect(page.locator("#articlePageInfo")).to_have_text("第 2 / 2 页")
    target = page.locator("#art-3064")
    expect(target).to_be_visible()
    expect(target).to_be_in_viewport()
    expect(target.locator(".no")).to_have_text("第五十一条")
    expect(target.locator(".reader-article-text")).to_have_text(
        FIXTURE["article_by_id"]["3064"]["text"]
    )
    expect(page.locator("#articleToc a[aria-current='location']")).to_have_text("第五十一条")


def verify_reader_search_click(page: Page) -> None:
    navigate_hash(page, "#/law/293")
    page.locator("#articleSearchInput").fill("商业秘密")
    page.locator("#btnArticleSearch").click()
    expect(page.locator("#articleSearchStatus")).to_have_text("共 5 条命中")
    hits = page.locator("#articleSearchResults .reader-search-hit")
    expect(hits).to_have_count(5)
    expect(hits.first).to_contain_text("第七条")
    hits.first.click()
    assert_hash(page, "#/law/293?article_no=第七条")
    target = page.locator("#art-13720")
    expect(target).to_be_visible()
    expect(target).to_be_in_viewport()
    expect(target.locator(".reader-article-text")).to_have_text(
        FIXTURE["article_by_id"]["13720"]["text"]
    )


def verify_real_relation_navigation(page: Page) -> None:
    navigate_hash(page, "#/law/216")
    page.locator("#tabRelations").click()
    expect(page.locator("#relationPanel")).to_be_visible()
    link = page.locator("#relationList .relation-item a").filter(
        has_text="上市公司信息披露暂缓与豁免管理规定"
    ).first
    expect(link).to_be_visible()
    link.click()
    assert_hash(page, "#/law/293")
    expect(page.locator("#lawHeader h2")).to_have_text(FIXTURE["laws"]["293"]["title"])


def verify_relation_1288_bidirectional(page: Page) -> None:
    navigate_hash(page, "#/law/215")
    page.locator("#tabRelations").click()
    outgoing = page.locator("#relationList .relation-item").filter(
        has_text="修改后的责令回购办法第十五条"
    )
    expect(outgoing).to_have_count(1)
    expect(page.locator("#relationList .rel-group-title")).to_contain_text("本法规 → 关联（出向）")
    outgoing_link = outgoing.locator("a")
    expect(outgoing_link).to_have_text("中华人民共和国证券法")
    outgoing_link.click()
    assert_hash(page, "#/law/20")
    expect(page.locator("#lawHeader h2")).to_have_text("中华人民共和国证券法")

    page.locator("#tabRelations").click()
    incoming = page.locator("#relationList .relation-item").filter(
        has_text="修改后的责令回购办法第十五条"
    )
    expect(incoming).to_have_count(1)
    expect(page.locator("#relationList .rel-group-title")).to_contain_text("被关联 → 本法规（入向）")
    incoming_link = incoming.locator("a")
    expect(incoming_link).to_have_text(FIXTURE["laws"]["215"]["title"])
    incoming_link.click()
    assert_hash(page, "#/law/215")
    expect(page.locator("#lawHeader h2")).to_have_text(FIXTURE["laws"]["215"]["title"])


def verify_wiki_tab_real_content(page: Page) -> None:
    navigate_hash(page, "#/law/293")
    page.locator("#tabRelations").click()
    page.locator("#tabWiki").click()
    expect(page.locator("#tabWiki")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#wikiPanel")).to_be_visible()
    wiki = FIXTURE["wikis"]["293"]
    actual = normalized_text(page.locator("#wikiBox").inner_text())
    for paragraph in wiki["markdown"].split("\n\n"):
        text = re.sub(r"^#{1,3}\s+", "", paragraph)
        text = re.sub(r"^[-*]\s+", "", text, flags=re.M)
        text = text.replace("**", "")
        assert normalized_text(text) in actual
    expect(page.locator("#btnWiki")).to_have_text("重新生成")


def verify_wiki_generation_blocked(page: Page) -> None:
    dialogs = []

    def record_dialog(dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", record_dialog)
    try:
        navigate_hash(page, "#/law/293")
        expect(page.locator("#wikiBox")).to_contain_text("第四条")
        button = page.locator("#btnWiki")
        expect(button).to_have_text("重新生成")
        button.click()
        notice = page.locator("#offlineWikiNotice")
        expect(notice).to_be_visible()
        expect(notice).to_have_text("完全离线演示不生成或重新生成解读")
        expect(page.locator("#wikiBox")).to_contain_text("第四条")
        expect(page.locator("#wikiBox")).not_to_contain_text("生成中")
        expect(button).to_be_enabled()
        assert dialogs == [], f"离线 Wiki 不应弹出确认框：{dialogs}"

        navigate_hash(page, "#/law/20")
        expect(button).to_have_text("生成解读")
        button.click()
        notice = page.locator("#offlineWikiNotice")
        expect(notice).to_be_visible()
        expect(notice).to_have_text("完全离线演示不生成或重新生成解读")
        expect(page.locator("#wikiBox")).not_to_contain_text("生成中")
        expect(page.locator("#wikiBox")).not_to_contain_text("30-60")
        expect(button).to_be_enabled()
        assert dialogs == [], f"离线 Wiki 不应弹出确认框：{dialogs}"
    finally:
        page.remove_listener("dialog", record_dialog)


def verify_graph_board_and_node_click(page: Page) -> None:
    if page.locator("#chatPanel").is_visible():
        page.locator("#btnMinChat").click()
    navigate_hash(page, "#/graph", "知识图谱")
    expect(page.locator("#graphBoards .gb-chip").first).to_be_visible()
    board = page.locator("#graphBoards .gb-chip").filter(has_text="信息披露 2")
    board.click()
    expect(page.locator("#graphMeta")).to_contain_text("信息披露 · 2 部法规")
    target = page.evaluate(
        """() => {
          const node = gActive.nodes.filter(n => n.id === 293)[0];
          if (!node) throw new Error('信息披露板块缺少真实节点 293');
          const point = toScreen(node), rect = gCanvas.getBoundingClientRect();
          return { x: rect.left + point.x, y: rect.top + point.y,
                   id: node.id, title: node.title };
        }"""
    )
    page.mouse.move(target["x"], target["y"])
    expect(page.locator("#graphTip")).to_be_visible()
    expect(page.locator("#graphTip")).to_contain_text(target["title"])
    page.mouse.click(target["x"], target["y"])
    assert_hash(page, "#/law/293")
    expect(page.locator("#lawHeader h2")).to_have_text(target["title"])


def verify_settings_offline_behavior(page: Page) -> None:
    page.locator("#btnSettings").click()
    expect(page.locator("#settingsModal")).to_be_visible()
    expect(page.locator("#cfgApiBase")).to_have_value("离线演示")
    inputs = page.locator("#settingsModal input")
    expect(inputs).to_have_count(5)
    for index in range(5):
        expect(inputs.nth(index)).to_have_attribute("readonly", "readonly")
        expect(inputs.nth(index)).to_have_attribute("aria-readonly", "true")
    page.locator("#btnSaveCfg").click()
    expect(page.locator("#cfgStatus")).to_contain_text("离线")
    expect(page.locator("#cfgStatus")).to_contain_text("不保存")
    page.locator("#btnTestConn").click()
    expect(page.locator("#cfgStatus")).to_contain_text("离线")
    expect(page.locator("#cfgStatus")).to_contain_text("不连接")
    page.locator("#btnCloseCfg").click()
    expect(page.locator("#settingsModal")).to_be_hidden()


def verify_official_link_guard(page: Page) -> None:
    navigate_hash(page, "#/law/20", "中华人民共和国证券法")
    before_hash = page.evaluate("() => location.hash")
    before_pages = len(page.context.pages)
    link = page.locator("#officialLawLink")
    expect(link).to_be_visible()
    assert (link.get_attribute("href") or "").startswith("https://")
    link.click()
    expect(page.locator("#offlineLinkNotice")).to_be_visible()
    expect(page.locator("#offlineLinkNotice")).to_have_text("需要联网打开官方原文")
    assert page.evaluate("() => location.hash") == before_hash
    assert len(page.context.pages) == before_pages


def verify_mobile_catalog(page: Page) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    try:
        navigate_hash(page, "#/laws", "全部法规")
        expect(page.locator("#sidebar")).to_have_class(re.compile(r"collapsed"))
        page.locator("#btnNavToggle").click()
        expect(page.locator("#sidebar")).not_to_have_class(re.compile(r"collapsed"))
        page.locator("#navLaws").click()
        expect(page.locator("#sidebar")).to_have_class(re.compile(r"collapsed"))
        expect(page.locator("#lawList .law-card")).to_have_count(6)
        page.locator("#pager a.page-link").filter(has_text="下一页").click()
        expect(page.locator("#lawList .law-card")).to_have_count(2)
        expect(page.locator("#pager .page-info")).to_have_text("第 2 / 2 页")
    finally:
        page.set_viewport_size({"width": 1440, "height": 900})


def verify_mobile_chat_and_evidence(page: Page) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    try:
        navigate_hash(page, "#/home", "证券法律法规研究")
        expect(page.locator("#homeRecentLaws a").first).to_be_visible()
        assert_no_horizontal_overflow(page, "移动375首页动态内容")
        navigate_hash(page, "#/search?q=减持计划", "减持计划")
        expect(page.locator("#searchArticleResults .search-hit").first).to_be_visible()
        assert_no_horizontal_overflow(page, "移动375搜索结果")
        navigate_hash(page, "#/law/216", "上市公司信息披露管理办法")
        expect(page.locator("#articleList .reader-article").first).to_be_visible()
        assert_no_horizontal_overflow(page, "移动375法规阅读器")
        navigate_hash(page, "#/graph", "知识图谱")
        expect(page.locator("#graphBoards .gb-chip").first).to_be_visible()
        assert_no_horizontal_overflow(page, "移动375知识图谱")
        navigate_hash(page, "#/home", "证券法律法规研究")
        preset = FIXTURE["chats"]["reduction"]
        answer = send_chat(page, preset["question"])
        assert_preset_answer(answer, preset)
        panel = page.locator("#chatPanel").bounding_box()
        assert panel is not None
        assert panel["x"] >= 0 and panel["x"] + panel["width"] <= 376
        answer.locator('.cite-reference[data-citation="0"]').click()
        expect(page.locator("#evidenceDrawer")).to_be_visible()
        article = FIXTURE["article_by_id"]["9349"]
        expect(page.locator("#evidenceBody .evidence-article h3")).to_have_text("第九条")
        expect(page.locator("#evidenceBody .evidence-article-text")).to_have_text(article["text"])
        drawer = page.locator("#evidenceDrawer").bounding_box()
        assert drawer is not None
        assert drawer["x"] >= 0 and drawer["x"] + drawer["width"] <= 376
        assert_no_horizontal_overflow(page, "移动375证据抽屉")
        expect(page.locator("#evidenceOpenReader")).to_be_visible()
        page.locator("#btnEvidenceClose").click()
        expect(page.locator("#evidenceDrawer")).to_be_hidden()
    finally:
        page.set_viewport_size({"width": 1440, "height": 900})


def run_deep_checks(page: Page) -> list[CheckResult]:
    checks = [
        ("深度 减持预置问答、证据与阅读器", verify_reduction_chat_evidence),
        ("深度 信息披露双预置引用、证据与阅读器", verify_disclosure_chat_evidence),
        ("深度 未预置问题示例与真实重试", verify_unknown_chat_and_retry),
        ("深度 当前法规跨范围问答拒绝", verify_current_law_scope_rejection),
        ("深度 非流式未知问题真实离线错误", verify_nonstream_unknown_chat_error),
        ("深度 聊天最小化拖动缩放与清空", verify_chat_panel_controls),
        ("深度 法规目录真实两页", verify_law_catalog_two_pages),
        ("深度 统一搜索全部法条法规范围", verify_search_scopes),
        ("深度 搜索筛选与清除", verify_search_filters),
        ("深度 搜索法规法条独立分页", verify_independent_search_pagination),
        ("深度 空搜索正式引导状态", verify_empty_search),
        ("深度 无结果词正式空状态", verify_no_result_search),
        ("深度 不存在法规正式错误状态", verify_missing_law_state),
        ("深度 证券法正文50加10两页", verify_securities_law_reader_pages),
        ("深度 证券法条号目录跨页定位", verify_securities_law_toc_target),
        ("深度 篇内搜索结果点击定位", verify_reader_search_click),
        ("深度 216真实关联导航", verify_real_relation_navigation),
        ("深度 关系1288双向真实导航", verify_relation_1288_bidirectional),
        ("深度 293 Wiki真实预生成内容", verify_wiki_tab_real_content),
        ("深度 Wiki生成与重新生成离线拦截", verify_wiki_generation_blocked),
        ("深度 图谱板块切换与真实节点点击", verify_graph_board_and_node_click),
        ("深度 设置五字段只读保存测试离线", verify_settings_offline_behavior),
        ("深度 官方外链只提示且保持hash", verify_official_link_guard),
        ("移动375 法规目录与分页", verify_mobile_catalog),
        ("移动375 聊天与证据抽屉", verify_mobile_chat_and_evidence),
    ]
    return [run_check(name, lambda check=check: check(page)) for name, check in checks]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME)
    parser.add_argument(
        "--routes-only", action="store_true", help="仅执行 5 项路由冒烟验收"
    )
    parser.add_argument(
        "--interactions", action="store_true",
        help="兼容模式：执行 5 项路由及 11 项基础交互，共 16 项",
    )
    parser.add_argument(
        "--deep", action="store_true",
        help="执行完整 41 项验收（与无参数默认相同，保留用于兼容旧 CI）",
    )
    parser.add_argument("--headed", action="store_true", help="显示 Chrome 窗口")
    parser.add_argument(
        "--timeout", type=int, default=5000, help="每个 Playwright 状态断言的超时毫秒数"
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    snapshot = args.snapshot.resolve()
    chrome = args.chrome
    if not snapshot.is_file():
        print(f"FATAL 快照不存在：{snapshot}")
        return 2
    if not chrome.is_file():
        print(f"FATAL 本机 Chrome 不存在：{chrome}")
        return 2
    if args.timeout <= 0:
        print("FATAL --timeout 必须是正整数")
        return 2

    print(f"快照：{snapshot}")
    print(f"Chrome：{chrome}")
    print("浏览器上下文：offline=True；不启动业务服务器")
    diagnostics = Diagnostics()
    results: list[CheckResult] = []

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                executable_path=str(chrome), headless=not args.headed
            )
        except PlaywrightError as error:
            print(f"FATAL Chrome 启动失败：{compact_error(error)}")
            return 2
        try:
            context = browser.new_context(
                offline=True, viewport={"width": 1440, "height": 900}
            )
            context.set_default_timeout(args.timeout)
            page = context.new_page()
            diagnostics.attach(page)
            page.goto(snapshot.as_uri(), wait_until="load", timeout=30_000)
            print("Chrome 启动与 file:// 页面加载成功\n")

            results.extend(run_route_checks(page))
            run_interactions = not args.routes_only or args.interactions or args.deep
            run_deep = args.deep or (not args.routes_only and not args.interactions)
            if run_interactions:
                results.extend(run_interaction_checks(page))
            if run_deep:
                results.extend(run_deep_checks(page))
            offline_errors = page.evaluate("() => window.offlineErrors || []")
            diagnostics.print_summary()
            print(f"  offlineErrors: {len(offline_errors)}")
            for item in offline_errors:
                print(f"    {item}")
            failures = [result.name for result in results if not result.passed]
            failures.extend(diagnostics.errors())
            failures.extend(f"存在 offlineErrors：{item}" for item in offline_errors)
            if failures:
                print(f"\n结果：FAIL（{len(failures)} 项失败）")
                for failure in failures:
                    print(f"  - {failure}")
                return 1
            print(f"\n结果：PASS（{len(results)} 项验收通过）")
            return 0
        except PlaywrightError as error:
            print(f"FATAL 浏览器执行失败：{compact_error(error)}")
            diagnostics.print_summary()
            return 2
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
