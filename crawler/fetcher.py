# crawler/fetcher.py — 限频抓取客户端：每站串行限频 + 抖动、指数退避、robots.txt、单日上限、原始留档
import hashlib
import random
import re
import time
import urllib.robotparser
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 LawKB-Research/1.0")
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
RETRY_DELAYS = (10, 30, 60)  # 指数退避（秒）
JITTER = (0.3, 1.5)


class RateLimiter:
    """每 host 独立串行限频：距上次请求不足 interval 时补足等待（含随机抖动）。"""

    def __init__(self, default_interval: float = 5.0):
        self.default_interval = default_interval
        self._last: dict[str, float] = {}

    def wait(self, host: str, interval: float | None = None) -> float:
        interval = interval if interval is not None else self.default_interval
        now = time.monotonic()
        last = self._last.get(host)
        if last is not None:
            need = last + interval + random.uniform(*JITTER) - now
            if need > 0:
                time.sleep(need)
        self._last[host] = time.monotonic()
        return self._last[host]


class Fetcher:
    """httpx 包装：限频 + 退避重试 + robots.txt + 单来源单日请求上限 + 原始留档。"""

    def __init__(
        self,
        rate_limiter: RateLimiter | None = None,
        daily_max: int = 500,
        archive_dir: Path | None = None,
    ):
        self.limiter = rate_limiter or RateLimiter()
        self.daily_max = daily_max
        self.archive_dir = archive_dir
        self._client = httpx.Client(
            timeout=30, headers=BROWSER_HEADERS, follow_redirects=True
        )
        self._counts: dict[str, int] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._referer: dict[str, str] = {}  # host → 最后访问页（用作 Referer）

    # ---- 内部 ----
    def _host(self, url: str) -> str:
        return urlparse(url).netloc

    def _check_budget(self, source: str) -> None:
        if self._counts.get(source, 0) >= self.daily_max:
            raise RuntimeError(
                f"source '{source}' reached daily_max={self.daily_max}; 熔断本次运行"
            )

    def _check_robots(self, url: str) -> bool:
        host = self._host(url)
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = self._client.get(
                    f"{urlparse(url).scheme}://{host}/robots.txt"
                )
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp = None  # 无 robots 视为允许
            except httpx.HTTPError:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def _archive(self, source: str, url: str, resp: httpx.Response) -> None:
        if self.archive_dir is None:
            return
        d = Path(self.archive_dir) / source / date.today().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        name = hashlib.md5(url.encode()).hexdigest()[:16]
        (d / f"{name}.html").write_bytes(resp.content)

    def _bump(self, source: str, ok: bool) -> None:
        self._counts[source] = self._counts.get(source, 0) + 1
        if ok:
            self._consecutive_failures[source] = 0
        else:
            self._consecutive_failures[source] = (
                self._consecutive_failures.get(source, 0) + 1
            )

    def _tripped(self, source: str) -> bool:
        """连续 3 次失败 → 该来源本次运行熔断。"""
        return self._consecutive_failures.get(source, 0) >= 3

    # ---- 对外 ----
    def get(self, url: str, source: str, interval: float | None = None) -> httpx.Response:
        self._check_budget(source)
        if not self._check_robots(url):
            raise PermissionError(f"robots.txt disallows: {url}")
        if self._tripped(source):
            raise RuntimeError(f"source '{source}' 熔断（连续 3 次失败）")
        self.limiter.wait(self._host(url), interval)
        last_exc: Exception | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                headers = {}
                ref = self._referer.get(self._host(url))
                if ref:
                    headers["Referer"] = ref
                resp = self._client.get(url, headers=headers)
                self._referer[self._host(url)] = url
                if resp.status_code in (429, 503):
                    self._bump(source, ok=False)
                    raise httpx.HTTPStatusError(
                        f"rate-limited: {resp.status_code}", request=resp.request, response=resp
                    )
                self._bump(source, ok=True)
                self._archive(source, url, resp)
                return resp
            except (httpx.HTTPError, httpx.HTTPStatusError) as e:
                last_exc = e
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
        raise last_exc  # type: ignore[misc]

    def download_pdf(self, url: str, dest: Path, source: str, interval: float | None = None) -> Path:
        resp = self.get(url, source, interval)
        if resp.status_code != 200:
            raise RuntimeError(f"download failed {resp.status_code}: {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest

    def close(self) -> None:
        self._client.close()


def default_fetcher(config: dict, data_dir: Path) -> Fetcher:
    """按全局配置构造生产用 Fetcher（含留档目录）。"""
    return Fetcher(
        rate_limiter=RateLimiter(default_interval=float(config.get("request_interval_sec", 5))),
        daily_max=int(config.get("daily_max_per_source", 500)),
        archive_dir=Path(data_dir) / "raw",
    )
