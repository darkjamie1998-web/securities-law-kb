# crawler/browser_fetcher.py — Playwright 无头浏览器抓取（攻克 JS 动态渲染站点）
# 仅用于列表页渲染；详情页/PDF 仍走 httpx（更快更省资源）。
# 合规：与 httpx 客户端共享 RateLimiter，并在导航前检查 robots.txt。
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class BrowserFetcher:
    """无头 Chromium：render(url) 返回 JS 渲染完成后的页面 HTML。

    惰性启动浏览器：构造时不拉起 Playwright，首次 render 才启动，
    便于单元测试注入假渲染函数、也避免无 render 来源时的无谓开销。
    """

    def __init__(self, rate_limiter=None, daily_max: int = 500,
                 robots_checker=None):
        self.limiter = rate_limiter
        self.daily_max = daily_max
        self.robots_checker = robots_checker
        self._count = 0
        self._pw = None
        self._browser = None
        self._page = None

    def _ensure_page(self):
        if self._page is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page(user_agent=BROWSER_UA)

    def _render(self, url: str, wait_selector: str | None, timeout: int) -> str:
        self._ensure_page()
        self._page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        if wait_selector:
            try:
                self._page.wait_for_selector(wait_selector, timeout=timeout)
            except Exception:
                pass  # 选择器未出现也返回当前 DOM（可能空列表）
        else:
            self._page.wait_for_load_state("networkidle", timeout=timeout)
        return self._page.content()

    def render(self, url: str, source: str, wait_selector: str | None = None,
               timeout: int = 20000) -> str:
        """打开页面等待渲染，返回渲染后的完整 HTML。"""
        if self._count >= self.daily_max:
            raise RuntimeError(f"browser fetch reached daily_max={self.daily_max}")
        if self.robots_checker and not self.robots_checker(url):
            raise PermissionError(f"robots.txt disallows: {url}")
        if self.limiter:
            self.limiter.wait(urlparse(url).netloc)
        self._count += 1
        return self._render(url, wait_selector, timeout)

    def close(self) -> None:
        try:
            if self._page:
                self._page.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
