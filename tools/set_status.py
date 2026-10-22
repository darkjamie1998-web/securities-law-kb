# tools/set_status.py — 人工维护法规时效状态（来源暂不提供状态时的兜底）
# 用法：
#   python tools/set_status.py --list
#   python tools/set_status.py --id 123 --status 已废止
#   python tools/set_status.py --title 证券法 --status 现行有效 --dry-run
import argparse
import sqlite3
from datetime import datetime

from tools.data_cleanup import DB_PATH, backup_db

VALID = ("现行有效", "已修订", "已废止")


def set_status(db, law_id=None, title=None, status="现行有效",
               dry_run: bool = False) -> list[dict]:
    """修改单部现行版本的状态；返回变更明细。"""
    if status not in VALID:
        raise ValueError(f"非法状态：{status}，可选 {VALID}")
    sql = ("SELECT id, title, status FROM laws "
           "WHERE duplicate_of IS NULL AND superseded_by IS NULL")
    params: list = []
    if law_id is not None:
        sql += " AND id = ?"
        params.append(law_id)
    elif title:
        sql += " AND title LIKE ?"
        params.append(f"%{title}%")
    else:
        raise ValueError("必须指定 --id 或 --title")
    rows = db.execute(sql, params).fetchall()
    if not rows:
        return []
    if len(rows) > 1:
        raise ValueError(f"匹配到 {len(rows)} 部法规，请用 --id 精确指定")
    row = rows[0]
    if not dry_run and row["status"] != status:
        db.execute(
            "UPDATE laws SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now().isoformat(timespec="seconds"), row["id"]))
        db.commit()
    return [{"id": row["id"], "title": row["title"],
             "old": row["status"], "new": status}]


def main():
    ap = argparse.ArgumentParser(description="人工维护法规时效状态（自动备份）")
    ap.add_argument("--id", type=int, help="法规 id")
    ap.add_argument("--title", help="法规标题关键词（必须唯一匹配）")
    ap.add_argument("--status", choices=VALID, default="现行有效")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="查看状态分布")
    args = ap.parse_args()

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        if args.list:
            for r in db.execute(
                    "SELECT status, count(*) AS n FROM laws "
                    "WHERE duplicate_of IS NULL AND superseded_by IS NULL "
                    "GROUP BY status ORDER BY n DESC"):
                print(f"{r['status']}: {r['n']}")
            return
        if not args.id and not args.title:
            ap.error("必须指定 --id 或 --title（或使用 --list）")
        if not args.dry_run:
            print(f"[backup] {backup_db()}")
        changed = set_status(db, args.id, args.title, args.status, args.dry_run)
        if not changed:
            print("未找到匹配的现行版本法规")
            return
        for c in changed:
            print(f"[{ 'dry-run' if args.dry_run else 'updated' }] "
                  f"{c['id']} {c['title'][:40]}：{c['old']} -> {c['new']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
