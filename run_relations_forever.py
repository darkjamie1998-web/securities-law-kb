# run_relations_forever.py — 图谱全量生成长跑脚本（独立于会话，可中断重跑）
# 用法：python run_relations_forever.py [--batch 50] [--sleep 5]
# 每批处理 --batch 部未抽取法规，直到全部完成；中断后重跑自动续。
import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_config
from app.db import get_db
from app.llm import LLMClient
from app.relations import process_law

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def pending_count(db: sqlite3.Connection) -> int:
    return db.execute(
        """SELECT count(*) FROM laws l WHERE NOT EXISTS (
             SELECT 1 FROM relations r JOIN articles a ON a.id = r.from_id
             WHERE a.law_id = l.id)"""
    ).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--sleep", type=int, default=5, help="批间隔秒数")
    args = ap.parse_args()

    db = get_db(DATA_DIR / "knowledge.db")
    llm = LLMClient(load_config(DATA_DIR))

    batch_no = 0
    while True:
        rows = db.execute(
            """SELECT l.id, l.title FROM laws l
               WHERE NOT EXISTS (
                 SELECT 1 FROM relations r JOIN articles a ON a.id = r.from_id
                 WHERE a.law_id = l.id)
               ORDER BY l.id LIMIT ?""",
            (args.batch,),
        ).fetchall()
        if not rows:
            print(f"[done] {datetime.now():%H:%M:%S} 全部法规处理完成！")
            return
        batch_no += 1
        print(f"[batch {batch_no}] {datetime.now():%H:%M:%S} "
              f"本批 {len(rows)} 部，剩余 {pending_count(db)} 部未处理", flush=True)
        ok = fail = 0
        for r in rows:
            try:
                result = process_law(db, llm, r["id"])
                print(f"  [{r['id']}] {result.get('title', r['title'])[:30]} "
                      f"extracted={result.get('extracted')} saved={result.get('saved')}",
                      flush=True)
                ok += 1
            except Exception as e:
                # 超时/内容过滤等：跳过，下轮循环会重试（幂等）
                print(f"  [{r['id']}] {r['title'][:30]} 失败（下轮重试）: {str(e)[:120]}",
                      flush=True)
                fail += 1
        print(f"[batch {batch_no}] 完成 ok={ok} fail={fail}", flush=True)
        if ok == 0:
            # 整批无进展（如网关持续超时/故障）：退出避免死循环，稍后重跑即可续
            print("[exit] 本批全部失败，可能网关异常，稍后重跑本脚本续跑", flush=True)
            return
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
