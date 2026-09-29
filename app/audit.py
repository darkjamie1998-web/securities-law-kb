# app/audit.py — 关系图谱完整性审计（CLI）
# 用法：
#   python -m app.audit            # 输出审计报告
#   python -m app.audit --fix     # 顺带修复"幽灵标记"（清除后由图谱 runner 重抽恢复入向边）
#
# 背景：历史版本 sync 更新法规时删除入向边但不失效引用方标记，导致
# "标记 saved>0 但实际出边为 0"的幽灵标记——这些法规不会被重抽，边永久丢失。
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def audit(db: sqlite3.Connection) -> dict:
    """三类完整性检查，返回结构化结果。"""
    # 1) 对称冗余：A→B 与 B→A 同 rel_type 并存（"修订替代/同一事项"语义对称，
    #    双向各存一条属于抽取冗余——展示无害，但提示可清理）
    redundant = db.execute(
        """SELECT r1.from_id, r1.to_id, r1.rel_type
           FROM relations r1
           WHERE r1.from_id < r1.to_id AND EXISTS (
             SELECT 1 FROM relations r2
             WHERE r2.from_id = r1.to_id AND r2.to_id = r1.from_id
               AND r2.rel_type = r1.rel_type)"""
    ).fetchall()

    # 2) 孤立节点：0 度法规（无出边也无入边）
    isolated = db.execute(
        """SELECT l.id, l.title FROM laws l
           WHERE NOT EXISTS (
             SELECT 1 FROM relations r
             JOIN articles a ON a.id = r.from_id OR a.id = r.to_id
             WHERE a.law_id = l.id)"""
    ).fetchall()

    # 3) 幽灵标记：标记声称 saved>0 但该法规实际出边为 0
    #    （入向边被 sync 删除、引用方标记未失效的历史痕迹）
    ghost = db.execute(
        """SELECT e.law_id, e.saved FROM relation_extractions e
           WHERE e.saved > 0 AND NOT EXISTS (
             SELECT 1 FROM relations r JOIN articles a ON a.id = r.from_id
             WHERE a.law_id = e.law_id)"""
    ).fetchall()

    total_laws = db.execute("SELECT count(*) FROM laws").fetchone()[0]
    total_edges = db.execute("SELECT count(*) FROM relations").fetchone()[0]
    marks = db.execute(
        "SELECT status, count(*) FROM relation_extractions GROUP BY status").fetchall()
    return {
        "total_laws": total_laws, "total_edges": total_edges,
        "marks": dict(marks),
        "redundant": [dict(r) for r in redundant],
        "isolated": [dict(r) for r in isolated],
        "ghost": [dict(r) for r in ghost],
    }


def fix_ghost_marks(db: sqlite3.Connection) -> int:
    """清除幽灵标记（saved>0 但实际出边 0），让这些法规回到 pending 重抽。"""
    ghost_ids = [r["law_id"] for r in db.execute(
        """SELECT e.law_id FROM relation_extractions e
           WHERE e.saved > 0 AND NOT EXISTS (
             SELECT 1 FROM relations r JOIN articles a ON a.id = r.from_id
             WHERE a.law_id = e.law_id)""").fetchall()]
    if not ghost_ids:
        return 0
    db.execute("DELETE FROM relation_extractions WHERE law_id IN (%s)" %
               ",".join(str(i) for i in ghost_ids))
    db.commit()
    return len(ghost_ids)


def main():
    ap = argparse.ArgumentParser(description="关系图谱完整性审计")
    ap.add_argument("--fix", action="store_true",
                    help="修复幽灵标记（清除后由图谱 runner 重抽恢复）")
    args = ap.parse_args()
    db = get_db(DATA_DIR / "knowledge.db")

    r = audit(db)
    print(f"=== 关系图谱审计 ===")
    print(f"法规 {r['total_laws']} 部 | 关系 {r['total_edges']} 条 | 标记 {r['marks']}")
    print(f"对称冗余边对: {len(r['redundant'])}（A→B 与 B→A 并存，展示无害，可忽略）")
    if r["redundant"]:
        for x in r["redundant"][:5]:
            print(f"  e.g. article {x['from_id']} ↔ {x['to_id']} ({x['rel_type']})")
    print(f"孤立节点（0 度法规）: {len(r['isolated'])}")
    for x in r["isolated"][:5]:
        print(f"  e.g. [{x['id']}] {x['title'][:30]}")
    if len(r["isolated"]) > 5:
        print(f"  … 共 {len(r['isolated'])} 部")
    print(f"幽灵标记（声称有关系实际为 0 出边）: {len(r['ghost'])}")
    for x in r["ghost"][:5]:
        print(f"  e.g. law {x['law_id']}（标记 saved={x['saved']}）")

    if args.fix and r["ghost"]:
        n = fix_ghost_marks(db)
        print(f"\n[fix] 已清除 {n} 个幽灵标记，相关法规将回到 pending 由重抽恢复入向边")
        print("      运行 relations.bat 或 python run_relations_forever.py 续跑")


if __name__ == "__main__":
    main()
