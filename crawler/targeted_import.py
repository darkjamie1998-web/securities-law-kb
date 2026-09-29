# crawler/targeted_import.py — 定向法律补充入库（官方刊载页 URL 清单驱动）
# 用途：按 crawler/targets.yaml 清单抓取指定法律的官方全文页并入库。
# 典型场景：补充税收/商业/公司类法律（深交所/上交所栏目未覆盖的通用商法经济法）。
#
# 用法：
#   python -m crawler.targeted_import                # 导入全部清单
#   python -m crawler.targeted_import --only 税收征收管理法   # 只导一部（标题模糊匹配）
#   python -m crawler.targeted_import --dry-run      # 只解析不入库，看效果
#
# 解析策略：标题锚定法 —— 在页面全文中定位"标题 + 紧邻正文特征（第X条/（…通过））"
# 的位置作为正文起点，遇导航/页脚终止词截断。不依赖各站 DOM 结构，
# 对 gov.cn / chinatax.gov.cn 等服务端渲染的官方刊载页通用。
import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.db import get_db, init_db
from crawler.fetcher import default_fetcher
from crawler.parsers import extract_text_from_html
from crawler.sync import upsert_law

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TARGETS = ROOT / "crawler" / "targets.yaml"

# 域名 → 来源名（写入 laws.source_name）
HOST_SOURCE = {
    "www.gov.cn": "中国政府网",
    "www.chinatax.gov.cn": "国家税务总局",
    "www.npc.gov.cn": "中国人大网",
    "flk.npc.gov.cn": "国家法律法规数据库",
}

# 正文终止词：命中即截断（页脚/推荐区/导航）
STOP_RE = re.compile(
    r"相关链接|相关政策|政策解读|打印本页|打印|关闭窗口|下一篇|上一篇|"
    r"主办[：:]|承办[：:]|网站标识码|扫一扫|网站地图|版权所有|"
    r"【[大字中字小字]】|放大|缩小|默认")

ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百千零]+条")
META_RE = re.compile(r"（[^（）]{4,80}(通过|修正|修订|公布|批准)")


def anchor_parse(html: str, title: str) -> tuple[str, str]:
    """标题锚定解析：返回 (full_text, issue_date)。

    在整页文本中寻找"标题出现且其后 400 字符内有正文特征"的位置，
    排除《XX实施条例/实施细则》等衍生文档的同名前缀，并越过 <title> 行
    与面包屑导航，推进到正文区的标题行。找不到锚点时回退整页文本
    （入库前由长度阈值与条文结构把关）。
    """
    text = extract_text_from_html(html)
    issue_date = ""
    start = -1
    for m in re.finditer(re.escape(title), text):
        # 跳过衍生文档与文内引用：标题紧跟"实施条例/实施细则"（如《招标投标法
        # 实施条例》），或紧跟"》"（正文里"根据《XX法》的规定"的引用处）
        tail = text[m.end(): m.end() + 5]
        if tail.startswith(("实施条例", "实施细则", "》")):
            continue
        window = text[m.end(): m.end() + 400]
        if ARTICLE_RE.search(window) or META_RE.search(window):
            start = m.start()
            meta = META_RE.search(text[m.end(): m.end() + 300])
            if meta:
                issue_date = meta.group(0).strip("（）")
            break
    if start < 0:
        # 回退：标题最后一次出现（比第一次更接近正文区）
        start = text.rfind(title)
        if start < 0:
            return "", ""
    # 锚点推进：首个命中常是 <title> 行（"XX法_中国人大网"），其后隔着面包屑。
    # 从元数据行/首条回溯最近的标题出现，把起点推进到正文区的标题行。
    anchor_end = start + len(title)
    mm = (META_RE.search(text[anchor_end: anchor_end + 600])
          or ARTICLE_RE.search(text[anchor_end: anchor_end + 1500]))
    if mm:
        pos = anchor_end + mm.start()
        t_pos = text.rfind(title, start + 1, pos)
        if t_pos > start:
            start = t_pos
    body = text[start:]
    stop = STOP_RE.search(body[20:])   # 跳过标题本身，避免标题含终止词误截
    if stop:
        body = body[: stop.start() + 20]
    return body.strip(), issue_date


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser(description="定向法律补充入库")
    ap.add_argument("--only", help="仅导入标题包含该关键词的法规")
    ap.add_argument("--dry-run", action="store_true", help="只解析不入库")
    args = ap.parse_args()

    with open(TARGETS, encoding="utf-8") as f:
        targets = yaml.safe_load(f)["laws"]
    if args.only:
        targets = [t for t in targets if args.only in t["title"]]
    if not targets:
        print("清单为空或 --only 无匹配")
        return

    db = get_db(DATA_DIR / "knowledge.db")
    init_db(db)
    cfg = load_config(DATA_DIR)
    fetcher = default_fetcher(cfg, DATA_DIR)
    browser = None   # 惰性启动：仅清单里有 render 条目时才拉起 Playwright

    def fetch_html(url: str, source_name: str, use_render: bool) -> str | None:
        """httpx 优先；render 条目或 httpx 拿到疑似反爬 stub 时用 Playwright 渲染。"""
        nonlocal browser
        if not use_render:
            resp = fetcher.get(url, source_name, interval=6)
            if resp.status_code == 200 and len(resp.text) > 4000:
                return resp.text
        if browser is None:
            from crawler.browser_fetcher import BrowserFetcher
            browser = BrowserFetcher()
        return browser.render(url, source_name, timeout=25000)

    ok = fail = skip = 0
    try:
        for t in targets:
            host = host_of(t["url"])
            source_name = HOST_SOURCE.get(host, host or "定向补充")
            try:
                html = fetch_html(t["url"], source_name, bool(t.get("render")))
                if not html:
                    print(f"  [fail] {t['title']}: 抓取为空")
                    fail += 1
                    continue
                full_text, issue_date = anchor_parse(html, t["title"])
                if len(full_text) < 200 or "第一条" not in full_text[:3000]:
                    print(f"  [fail] {t['title']}: 解析结果异常（{len(full_text)} 字，无条文结构）")
                    fail += 1
                    continue
                doc = {"title": t["title"], "full_text": full_text,
                       "source_url": t["url"], "issue_date": issue_date or None}
                if args.dry_run:
                    print(f"  [dry ] {t['title']}: {len(full_text)} 字，"
                          f"开头: {full_text[:60]!r}")
                    continue
                action = upsert_law(db, {"name": source_name, "level": t.get("level", "法律")}, doc)
                db.commit()
                print(f"  [{action}] {t['title']}（{len(full_text)} 字）")
                ok += 1 if action in ("new", "updated") else 0
                skip += 1 if action == "skip" else 0
            except Exception as e:
                db.rollback()
                print(f"  [fail] {t['title']}: {str(e)[:120]}")
                fail += 1
    finally:
        if browser is not None:
            browser.close()
    if not args.dry_run:
        print(f"\n完成：新增/更新 {ok}，跳过 {skip}，失败 {fail}")
        print("下一步：python run_relations_forever.py 对新法规生成关联（自动只处理 pending）")


if __name__ == "__main__":
    main()
