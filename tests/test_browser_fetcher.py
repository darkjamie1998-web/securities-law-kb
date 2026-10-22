"""浏览器抓取合规测试：robots 检查、共享限频、单日上限（不启动真实浏览器）。"""
import pytest

from crawler.browser_fetcher import BrowserFetcher


class FakeLimiter:
    def __init__(self):
        self.hosts = []

    def wait(self, host, interval=None):
        self.hosts.append(host)


def test_render_checks_robots_and_limiter(monkeypatch):
    limiter = FakeLimiter()
    calls = {"robots": 0}

    def robots(url):
        calls["robots"] += 1
        return True

    bf = BrowserFetcher(rate_limiter=limiter, daily_max=10, robots_checker=robots)
    monkeypatch.setattr(bf, "_render", lambda url, wait_selector, timeout: "<html/>")
    assert bf.render("https://example.com/a", "src") == "<html/>"
    assert calls["robots"] == 1
    assert limiter.hosts == ["example.com"]
    bf.close()


def test_render_rejects_robots_denied():
    bf = BrowserFetcher(rate_limiter=FakeLimiter(), robots_checker=lambda url: False)
    with pytest.raises(PermissionError):
        bf.render("https://example.com/a", "src")
    bf.close()


def test_daily_max():
    bf = BrowserFetcher(rate_limiter=FakeLimiter(), daily_max=0)
    with pytest.raises(RuntimeError):
        bf.render("https://example.com/a", "src")
    bf.close()
