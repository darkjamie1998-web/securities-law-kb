# crawler/sync.py — 增量/全量同步编排
# 用法：
#   python -m crawler.sync                          # 全部启用来源增量
#   python -m crawler.sync --source 证监会           # 单来源
#   python -m crawler.sync --source 深圳证券交易所 --limit 20   # 小规模试跑
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.db import get_db, init_db
from crawler.fetcher import default_fetcher
from crawler.parsers import (
    content_hash,
    extract_list,
    extract_text_from_file,
    parse_law,
    split_articles,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def load_sources() -> list[dict]:
    with open(ROOT / "crawler" / "sources.yaml", encoding="utf-8") as f:
        return [s for s in yaml.safe_load(f)["sources"] if s.get("enabled")]


def upsert_law(db: sqlite3.Connection, source: dict, doc: dict) -> str:
    """入库/更新一部法规及其法条。返回 'new' / 'updated' / 'skip'。"""
    title = doc.get("title", "").strip()
    full_text = doc.get("full_text", "").strip()
    if not title or len(full_text) < 50:
        return "skip"
    chash = content_hash(full_text)
    now = datetime.now().isoformat(timespec="seconds")

    row = db.execute(
        "SELECT id, content_hash FROM laws WHERE source_name=? AND title=?",
        (source["name"], title),
    ).fetchone()
    if row and row["content_hash"] == chash:
        return "skip"
    if row:
        law_id = row["id"]
        db.execute(
            """UPDATE laws SET doc_number=?, issuer=?, issue_date=?, effective_date=?,
               content_hash=?, full_text=?, updated_at=? WHERE id=?""",
            (
                doc.get("doc_number"), doc.get("issuer"), doc.get("issue_date"),
                doc.get("effective_date"), chash, full_text, now, law_id,
            ),
        )
        action = "updated"
    else:
        try:
            cur = db.execute(
                """INSERT INTO laws(title, doc_number, issuer, level, issue_date,
                   effective_date, source_name, source_url, content_hash, full_text,
                   created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    title, doc.get("doc_number"), doc.get("issuer"), source.get("level", ""),
                    doc.get("issue_date"), doc.get("effective_date"), source["name"],
                    doc.get("source_url"), chash, full_text, now, now,
                ),
            )
            law_id = cur.lastrowid
            action = "new"
        except sqlite3.IntegrityError:
            # 同内容 PDF 挂在多个栏目（不同 source_name）：视为已入库，跳过
            db.rollback()
            return "skip"

    # 内容变更时清理派生数据：relations 的 FK（无 ON DELETE）会阻断
    # DELETE articles，必须先删关系；抽取标记与 wiki 缓存基于旧内容，一并失效
    # 入向边的引用方：它们的标记也要连带失效——否则引用方（标记仍是 ok）
    # 不会重抽，指向本法规的边永久丢失。必须在删除关系前查出引用方。
    referencing = db.execute(
        """SELECT DISTINCT fa.law_id FROM relations r
           JOIN articles fa ON fa.id = r.from_id
           JOIN articles ta ON ta.id = r.to_id
           WHERE ta.law_id = ?""",
        (law_id,),
    ).fetchall()
    db.execute(
        """DELETE FROM relations WHERE from_id IN
             (SELECT id FROM articles WHERE law_id=?)
           OR to_id IN (SELECT id FROM articles WHERE law_id=?)""",
        (law_id, law_id),
    )
    db.execute("DELETE FROM wiki_pages WHERE law_id=?", (law_id,))
    db.execute("DELETE FROM relation_extractions WHERE law_id=?", (law_id,))
    if referencing:
        db.execute(
            "DELETE FROM relation_extractions WHERE law_id IN (%s)" %
            ",".join(str(r["law_id"]) for r in referencing),
        )
    # 法条重建（FTS 触发器自动同步）
    db.execute("DELETE FROM articles WHERE law_id=?", (law_id,))
    arts = split_articles(full_text)
    if not arts:
        # 无"条"结构（监管问答等）：整篇作为单条
        arts = [("", full_text)]
    for no, text in arts:
        db.execute(
            "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
            (law_id, no, text),
        )
    return action


def _iter_list_pages(fetcher, source: dict):
    """产出各列表页 HTML：szse 方法自动翻页（index_N.html），其他来源只取第一页。"""
    from urllib.parse import urljoin

    url = source["list_url"]
    page = 0
    while True:
        resp = fetcher.get(url, source["name"],
                           interval=source.get("interval_sec"))
        if resp.status_code != 200:
            if page > 0:
                return  # 翻页到头（404），正常结束
            raise RuntimeError(f"列表页请求失败 {resp.status_code}: {url}")
        yield resp.text
        if source.get("method") != "szse":
            return
        page += 1
        url = urljoin(source["list_url"], f"index_{page}.html")


def sync_source(
    db: sqlite3.Connection, fetcher, source: dict, limit: int | None = None,
    browser=None,
) -> dict:
    started = datetime.now().isoformat(timespec="seconds")
    new = updated = skipped = failed = 0
    errors = []
    try:
        if source.get("render") and browser is not None:
            # JS 动态渲染站点：列表页走 Playwright（详情仍走 httpx）
            html = browser.render(source["list_url"], source["name"],
                                  wait_selector=source.get("wait_selector"))
            links = extract_list(html, source["list_url"],
                                 source.get("method", "generic"))
            if limit:
                links = links[:limit]
        else:
            links = []
            seen = set()
            for html in _iter_list_pages(fetcher, source):
                items = extract_list(html, source["list_url"],
                                     source.get("method", "generic"))
                fresh = [(t, u) for t, u in items if u not in seen]
                if not fresh and len(links) > 0:
                    break  # 空页=翻页结束
                for _, u in fresh:
                    seen.add(u)
                links.extend(fresh)
                if limit and len(links) >= limit:
                    links = links[:limit]
                    break
        if limit:
            links = links[:limit]
        for text, url in links:
            try:
                low = url.lower()
                if low.endswith((".pdf", ".docx", ".doc")):
                    # 附件正文（PDF/Word）：下载后提取文本，标题用列表页名称
                    ext = low[low.rfind("."):]
                    file_path = DATA_DIR / "raw" / "files" / (
                        content_hash(url) + ext
                    )
                    fetcher.download_pdf(url, file_path, source["name"],
                                         interval=source.get("interval_sec"))
                    doc = {
                        "title": text,
                        "full_text": extract_text_from_file(file_path),
                        "source_url": url,
                    }
                else:
                    detail = fetcher.get(url, source["name"],
                                         interval=source.get("interval_sec"))
                    if detail.status_code != 200:
                        failed += 1
                        continue
                    doc = parse_law(detail.text, url,
                                    source.get("method", "generic"))
                    doc.setdefault("title", text)
                    if not doc.get("title"):
                        doc["title"] = text
                action = upsert_law(db, source, doc)
                if action == "new":
                    new += 1
                elif action == "updated":
                    updated += 1
                else:
                    skipped += 1
                db.commit()
            except Exception as e:  # 单条失败不阻断整体
                db.rollback()  # 回滚悬挂的 UPDATE/DELETE，避免半提交的不一致状态
                failed += 1
                errors.append(f"{url}: {e}")
        status = "ok" if not errors else "partial"
    except Exception as e:
        status = "failed"
        errors.append(str(e))
    finished = datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO sync_log(source_name, started_at, finished_at, new_count,"
        " updated_count, status, detail) VALUES(?,?,?,?,?,?,?)",
        (source["name"], started, finished, new, updated, status,
         "; ".join(errors[:20])),
    )
    db.commit()
    return {"new": new, "updated": updated, "skipped": skipped,
            "failed": failed, "status": status, "errors": errors}


def main():
    ap = argparse.ArgumentParser(description="法律法规知识库增量同步")
    ap.add_argument("--source", help="仅同步指定名称的来源")
    ap.add_argument("--limit", type=int, help="最多处理的详情页数量（试跑用）")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    db = get_db(DATA_DIR / "knowledge.db")
    init_db(db)
    cfg = load_config(DATA_DIR)
    fetcher = default_fetcher(cfg, DATA_DIR)

    sources = load_sources()
    if args.source:
        sources = [s for s in sources if s["name"] == args.source]
        if not sources:
            print(f"未找到来源：{args.source}（检查 crawler/sources.yaml 的 enabled/name）")
            return

    # 有 render 来源时才启动无头浏览器（懒加载，避免无谓开销）
    browser = None
    if any(s.get("render") for s in sources):
        from crawler.browser_fetcher import BrowserFetcher
        from crawler.fetcher import RateLimiter
        browser = BrowserFetcher(
            rate_limiter=RateLimiter(default_interval=float(cfg.get("request_interval_sec", 5))),
            daily_max=int(cfg.get("daily_max_per_source", 500)),
        )

    try:
        for s in sources:
            print(f"[sync] {s['name']} …", flush=True)
            r = sync_source(db, fetcher, s, args.limit, browser=browser)
            print(f"[sync] {s['name']}: new={r['new']} updated={r['updated']} "
                  f"skipped={r['skipped']} failed={r['failed']} status={r['status']}")
            for e in r["errors"][:5]:
                print(f"       {e}")
    finally:
        if browser:
            browser.close()
        fetcher.close()


if __name__ == "__main__":
    main()
