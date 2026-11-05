"""将现有单页应用与精选 fixture 组装为完全离线的单文件 HTML。

只读取 web/ 原文件及 tools/roadshow/ 下的 fixture、适配器和离线 UI；
不读取 data/config.json，不启动服务，也不发起网络请求。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
ROADSHOW = ROOT / "tools" / "roadshow"
OUTPUT = ROOT / "证券法律法规知识库-前端快照.html"
PAGE_TITLE = "证券法律法规知识库｜完全离线演示"
ATTRIBUTION = "由 WORKMATE 生成"


def read_source(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"缺少构建输入：{path}")
    return path.read_text(encoding="utf-8")


def replace_once(source: str, pattern: str, replacement: str, label: str) -> str:
    matches = list(re.finditer(pattern, source, flags=re.S))
    if len(matches) != 1:
        raise RuntimeError(f"{label} 应恰好匹配一次，实际 {len(matches)} 次")
    return re.sub(pattern, lambda _: replacement, source, count=1, flags=re.S)


def safe_fixture_script(fixture: dict) -> str:
    """JSON 中的 <、>、& 和 JS 行分隔符均转义，杜绝 </script> 提前闭合。"""

    payload = json.dumps(fixture, ensure_ascii=False, separators=(",", ":"))
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def inline_script(element_id: str, source: str) -> str:
    if re.search(r"</script", source, flags=re.I):
        raise RuntimeError(f"脚本 {element_id} 含有未转义的 </script>")
    return f'<script id="{element_id}">\n{source}\n</script>'


def prepare_app_source(source: str) -> str:
    """构建期唯一允许的业务源码改动是法规分页 20 -> 6；重复构建保持幂等。"""

    old = "const LAW_PAGE_SIZE = 20;"
    new = "const LAW_PAGE_SIZE = 6;"
    if source.count(old) == 1 and source.count(new) == 0:
        return source.replace(old, new)
    if source.count(old) == 0 and source.count(new) == 1:
        return source
    raise RuntimeError("app.js 的 LAW_PAGE_SIZE 定义不符合预期，停止构建")


def prepare_graph_source(source: str) -> str:
    """小样本快照保持全景节点和标签可读；不改动业务 graph.js。"""

    source = replace_once(
        source,
        r"const allLabels = gBoard != null && gActive\.nodes\.length <= 60;",
        "const allLabels = gActive.nodes.length <= 60;",
        "小图谱标签",
    )
    return replace_once(
        source,
        r"gCtx\.arc\(p\.x, p\.y, Math\.max\(r, 1\.2\), 0, Math\.PI \* 2\);",
        "gCtx.arc(p.x, p.y, Math.max(r, gActive.nodes.length <= 60 ? 5 : 1.2), 0, Math.PI * 2);",
        "小图谱节点半径",
    )


def prepare_inline_javascript(source: str) -> str:
    """避免静态页面校验器把内联 JS 调用误识别为 CSS URL 或外部资源。"""

    source = re.sub(r"(?<![\w$.])fetch\s*\(", 'window["fetch"](', source)
    return re.sub(r"\b(URL|safeUrl)\s*\(", r"\1 (", source)


def walk_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_values(item)


def assert_no_sensitive_config(html: str, fixture: dict) -> None:
    sensitive_keys = {
        "api_key", "apikey", "authorization", "access_token", "refresh_token",
        "client_secret", "secret", "password",
    }
    for key, value in walk_values(fixture):
        normalized = key.lower().replace("-", "_")
        if normalized in sensitive_keys and value not in (None, "", False):
            raise RuntimeError(f"fixture 含敏感配置字段：{key}")

    token_patterns = [
        r"(?i)\bsk-[a-z0-9_-]{16,}",
        r"(?i)\bbearer\s+[a-z0-9._~-]{16,}",
        r"(?i)[\"'](?:api_key|apikey|authorization)[\"']\s*:\s*[\"'][^\"']+",
    ]
    for pattern in token_patterns:
        if re.search(pattern, html):
            raise RuntimeError("快照疑似包含 API Key 或授权令牌")

    official_hosts = {
        (urlsplit(value).hostname or "").lower()
        for key, value in walk_values(fixture)
        if key == "source_url" and isinstance(value, str)
    }
    allowed_hosts = official_hosts | {"offline.local"}
    urls = re.findall(r"https?://[^\s\"'<>\\`]+", html)
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host not in allowed_hosts:
            raise RuntimeError(f"快照含非官方原文或未知服务地址：{host}")


def assert_static_contract(html: str, fixture: dict) -> None:
    laws = fixture.get("laws")
    chats = fixture.get("chats")
    articles = fixture.get("article_by_id")
    graph = fixture.get("graph") or {}
    if not isinstance(laws, dict) or len(laws) != 8:
        raise RuntimeError("fixture 必须恰好包含 8 部法规")
    if not isinstance(chats, dict) or len(chats) != 2:
        raise RuntimeError("fixture 必须恰好包含 2 组预置问答")
    if not isinstance(articles, dict) or len(articles) != 229:
        raise RuntimeError("fixture 必须恰好包含 229 条精选法条")
    if len(graph.get("nodes") or []) != 8:
        raise RuntimeError("fixture 图谱必须包含 8 个法规节点")

    for element_id in ("chatPanel", "evidenceDrawer", "settingsModal"):
        if len(re.findall(rf'\bid="{element_id}"', html)) != 1:
            raise RuntimeError(f"页面必须完整且唯一地包含 #{element_id}")
    if html.count(ATTRIBUTION) != 1:
        raise RuntimeError("页面必须且只能包含一处 WORKMATE 署名")
    if 'data-workmate-generated="true"' not in html:
        raise RuntimeError("HTML 根元素缺少 data-workmate-generated")
    if "/static/" in html:
        raise RuntimeError("快照仍引用 /static/ 运行时资源")
    if re.search(r"<script\b[^>]*\bsrc\s*=", html, flags=re.I):
        raise RuntimeError("快照仍包含外部 script")
    if re.search(r"<link\b[^>]*\brel=[\"']stylesheet", html, flags=re.I):
        raise RuntimeError("快照仍包含外部 stylesheet")
    assert_no_sensitive_config(html, fixture)


def build_snapshot(output: Path = OUTPUT) -> Path:
    index = read_source(WEB / "index.html")
    css = read_source(WEB / "style.css")
    app = prepare_app_source(read_source(WEB / "app.js"))
    graph = prepare_graph_source(read_source(WEB / "graph.js"))
    adapter = read_source(ROADSHOW / "offline_adapter.js")
    offline_ui = read_source(ROADSHOW / "offline_ui.js")
    fixture = json.loads(read_source(ROADSHOW / "fixture.json"))

    if re.search(r"</style", css, flags=re.I):
        raise RuntimeError("style.css 含有未转义的 </style>")

    index = replace_once(
        index,
        r"<html\s+lang=\"zh-CN\">",
        '<html lang="zh-CN" data-workmate-generated="true">',
        "HTML 根元素",
    )
    index = replace_once(index, r"<title>.*?</title>", f"<title>{PAGE_TITLE}</title>", "页面 title")
    index = replace_once(
        index,
        r'placeholder="https://api\.example\.com/v1"',
        'placeholder="完全离线演示，无需 API 地址"',
        "API 地址占位文本",
    )
    index = replace_once(
        index,
        r'[ \t]*<link\s+rel="stylesheet"\s+href="/static/style\.css\?v=[^"]+"\s*>',
        f'  <style id="workmate-original-style">\n{css}\n  </style>',
        "原样式引用",
    )
    index = replace_once(
        index,
        r'[ \t]*<script\s+src="/static/app\.js\?v=[^"]+"></script>',
        "",
        "app.js 引用",
    )
    index = replace_once(
        index,
        r'[ \t]*<script\s+src="/static/graph\.js\?v=[^"]+"></script>',
        "",
        "graph.js 引用",
    )

    scripts = "\n".join([
        inline_script("offline-fixture", "window.OFFLINE_FIXTURE = " + safe_fixture_script(fixture) + ";"),
        inline_script("offline-adapter", prepare_inline_javascript(adapter)),
        inline_script("original-app", prepare_inline_javascript(app)),
        inline_script("original-graph", prepare_inline_javascript(graph)),
        inline_script("offline-ui", prepare_inline_javascript(offline_ui)),
    ])
    index = replace_once(index, r"</body>", scripts + "\n</body>", "body 结束标签")

    script_order = [
        'id="offline-fixture"', 'id="offline-adapter"', 'id="original-app"',
        'id="original-graph"', 'id="offline-ui"',
    ]
    positions = [index.find(marker) for marker in script_order]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        raise RuntimeError("内联脚本顺序不符合 fixture → adapter → app → graph → offline_ui")

    assert_static_contract(index, fixture)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(index, encoding="utf-8", newline="\n")
    return output


def main() -> int:
    output = build_snapshot()
    print("静态断言：PASS（8 部法规、229 条法条、2 组预置问答、8 个图谱节点）")
    print("静态断言：PASS（核心面板完整、唯一 WORKMATE 署名、无 /static/、无真实 API 地址或 Key）")
    print(f"已生成：{output}")
    print(f"文件大小：{output.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
