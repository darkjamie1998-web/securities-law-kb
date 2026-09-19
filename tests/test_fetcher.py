# tests/test_fetcher.py — 限频逻辑测试（monkeypatch 时间，不真实等待、不联网）
import pytest

from crawler.fetcher import RateLimiter, Fetcher


def test_rate_limiter_first_call_no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    t = {"now": 100.0}
    monkeypatch.setattr("time.monotonic", lambda: t["now"])
    rl = RateLimiter(default_interval=5.0)
    rl.wait("site_a")
    assert slept == []  # 首次请求无需等待


def test_rate_limiter_enforces_interval(monkeypatch):
    slept = []
    t = {"now": 100.0}

    def fake_sleep(s):
        slept.append(s)
        t["now"] += s

    monkeypatch.setattr("time.sleep", fake_sleep)
    monkeypatch.setattr("time.monotonic", lambda: t["now"])
    rl = RateLimiter(default_interval=5.0)
    rl.wait("site_a")
    t["now"] += 1.0          # 距上次请求只过了 1s
    rl.wait("site_a")
    # 应补足等待：interval(5) - 已过(1) + 抖动(0.3~1.5) => 4.3 ~ 5.5
    assert slept and 4.2 <= slept[-1] <= 5.6


def test_rate_limiter_hosts_independent(monkeypatch):
    slept = []
    t = {"now": 100.0}
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("time.monotonic", lambda: t["now"])
    rl = RateLimiter(default_interval=5.0)
    rl.wait("site_a")
    rl.wait("site_b")        # 不同 host 不受 site_a 影响
    assert slept == []


def test_fetcher_respects_daily_max(monkeypatch):
    """单来源请求数达到 daily_max 后熔断。"""
    f = Fetcher(daily_max=2, rate_limiter=FakeNoWaitLimiter())

    class FakeResp:
        status_code = 200
        text = "<html>ok</html>"

    calls = []
    monkeypatch.setattr(
        f._client, "get", lambda url, **kw: calls.append(url) or FakeResp()
    )
    f.get("http://x/1", source="s")
    f.get("http://x/2", source="s")
    with pytest.raises(RuntimeError, match="daily_max"):
        f.get("http://x/3", source="s")


class FakeNoWaitLimiter:
    def wait(self, host, interval=None):
        pass
