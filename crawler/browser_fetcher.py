# crawler/browser_fetcher.py — Playwright 无头浏览器抓取（攻克 JS 动态渲染站点）
# 仅用于列表页渲染；详情页/PDF 仍走 httpx（更快更省资源）。
from playwright.sync_api import sync_playwright

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class BrowserFetcher:
    """无头 Chromium：render(url) 返回 JS 渲染完成后的页面 HTML。

    限频与单日上限与 Fetcher 同标准（共享 RateLimiter 语义）。
    """

    def __init__(self, rate_limiter=None, daily_max: int = 500):
        self.limiter = rate_limiter
        self.daily_max = daily_max
        self._count = 0
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._page = self._browser.new_page(user_agent=BROWSER_UA)

    def render(self, url: str, source: str, wait_selector: str | None = None,
               timeout: int = 20000) -> str:
        """打开页面等待渲染，返回渲染后的完整 HTML。"""
        if self._count >= self.daily_max:
            raise RuntimeError(f"browser fetch reached daily_max={self.daily_max}")
        if self.limiter:
            from urllib.parse import urlparse
            self.limiter.wait(urlparse(url).netloc)
        self._count += 1
        self._page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        if wait_selector:
            try:
                self._page.wait_for_selector(wait_selector, timeout=timeout)
            except Exception:
                pass  # 选择器未出现也返回当前 DOM（可能空列表）
        else:
            self._page.wait_for_load_state("networkidle", timeout=timeout)
        return self._page.content()

    def close(self) -> None:
        try:
            self._page.close()
            self._browser.close()
            self._pw.stop()
        except Exception:
            pass
