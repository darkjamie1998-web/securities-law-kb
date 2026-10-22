# tools/data_cleanup.py — 备份、去重、质量标记
# 安全策略：每次运行先 sqlite3.backup()；只标记不物理删除法规；空条文可删除。
import argparse
import difflib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "knowledge.db"
BACKUP_DIR = DATA_DIR / "backups"

# 来源权威性：数值越高越优先保留
SOURCE_RANK = {
    "国家法律法规数据库": 100, "中国人大网": 95, "中国政府网": 90,
    "最高人民法院": 80, "中国证监会": 70,
    "上海证券交易所": 60, "深圳证券交易所": 60, "北京证券交易所": 60,
}


def backup_db(src: Path = DB_PATH, dest_dir: Path = BACKUP_DIR) -> Path:
    """生成一致性备份（sqlite backup API，服务运行中也可安全执行）。"""
    if not Path(src).exists():
        raise FileNotFoundError(f"数据库不存在: {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"knowledge-{datetime.now():%Y%m%d-%H%M%S}.db"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return dest


def normalize_title(title: str) -> str:
    """去来源前缀、版本后缀与书名号，用于重复检测。"""
    t = re.sub(r"^【[^】]*】", "", title or "").strip()
    t = re.sub(r"[（(]\d{4}年(修订|修正)[）)]$", "", t).strip()
    if "《" in t and "》" in t:
        t = t[t.find("《") + 1:t.rfind("》")]
    return t.strip("《》").strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "")[:20000], (b or "")[:20000]).ratio()


def _rank(row) -> tuple:
    return (SOURCE_RANK.get(row["source_name"], 0), len(row["full_text"] or ""))


def dedupe(db, dry_run: bool = False) -> int:
    """同标题重复法规标记 duplicate_of；相似度 <0.95 的保留（可能是不同版本）。"""
    rows = db.execute(
        """SELECT id, title, full_text, source_name, issue_date, effective_date,
                  content_hash FROM laws
           WHERE duplicate_of IS NULL AND superseded_by IS NULL""").fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(normalize_title(r["title"]), []).append(r)
    changed = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group = sorted(group, key=_rank, reverse=True)
        keep = group[0]
        for other in group[1:]:
            if other["content_hash"] == keep["content_hash"] or \
               similarity(other["full_text"], keep["full_text"]) >= 0.95:
                if not dry_run:
                    db.execute(
                        "UPDATE laws SET duplicate_of=?, quality_flag='duplicate' WHERE id=?",
                        (keep["id"], other["id"]))
                changed += 1
    if not dry_run:
        db.commit()
    return changed


def mark_quality(db, dry_run: bool = False) -> int:
    """标记过短法规为 empty_text，并删除正文为空的条文。"""
    changed = 0
    for r in db.execute("SELECT id, full_text FROM laws").fetchall():
        if len((r["full_text"] or "").strip()) < 200:
            if not dry_run:
                db.execute("UPDATE laws SET quality_flag='empty_text' WHERE id=?", (r["id"],))
            changed += 1
    empty_ids = [r[0] for r in db.execute(
        "SELECT id FROM articles WHERE length(trim(text)) < 5")]
    if empty_ids and not dry_run:
        q = ",".join("?" * len(empty_ids))
        # seg 索引无触发器，删除条文时手动清掉对应行（表可能尚未建立）
        try:
            db.execute(f"DELETE FROM articles_seg WHERE rowid IN ({q})", empty_ids)
        except sqlite3.OperationalError:
            pass
        db.execute(f"DELETE FROM articles WHERE id IN ({q})", empty_ids)
    if not dry_run:
        db.commit()
    return changed + len(empty_ids)


def report(db) -> dict:
    return {
        "laws": db.execute("SELECT count(*) FROM laws").fetchone()[0],
        "duplicates": db.execute(
            "SELECT count(*) FROM laws WHERE duplicate_of IS NOT NULL").fetchone()[0],
        "empty_text": db.execute(
            "SELECT count(*) FROM laws WHERE quality_flag='empty_text'").fetchone()[0],
        "empty_articles": db.execute(
            "SELECT count(*) FROM articles WHERE length(trim(text)) < 5").fetchone()[0],
        "unsplit": db.execute(
            "SELECT count(*) FROM articles WHERE article_no IS NULL OR article_no=''").fetchone()[0],
    }


def main():
    ap = argparse.ArgumentParser(description="知识库数据治理（自动备份）")
    ap.add_argument("--backup-only", action="store_true")
    ap.add_argument("--dedupe", action="store_true")
    ap.add_argument("--mark-quality", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    backup = backup_db()
    print(f"[backup] {backup}")
    if args.backup_only:
        return

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        if args.dedupe:
            print("[dedupe] changed", dedupe(db, args.dry_run))
        if args.mark_quality:
            print("[quality] changed", mark_quality(db, args.dry_run))
        if args.report or not (args.dedupe or args.mark_quality):
            rep = report(db)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            if not args.dry_run:
                out = DATA_DIR / f"cleanup-report-{datetime.now():%Y%m%d}.json"
                out.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                print(f"[report] {out}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
