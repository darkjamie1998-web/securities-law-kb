# app/embed.py — 批量 embedding 回填（幂等，可中断重跑）
# 用法：python -m app.embed [--batch 64]
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.db import get_db
from app.llm import LLMClient, LLMError

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def backfill(db: sqlite3.Connection, client: LLMClient, batch: int = 64) -> dict:
    """扫描 embedding IS NULL 的法条，批量向量化写回。"""
    rows = db.execute(
        "SELECT id, text FROM articles WHERE embedding IS NULL ORDER BY id"
    ).fetchall()
    total = len(rows)
    done = skipped = 0
    for i in range(0, total, batch):
        chunk = rows[i:i + batch]
        try:
            vecs = client.embed([r["text"] for r in chunk], batch=batch)
        except LLMError as e:
            print(f"[embed] 批次失败（跳过 {len(chunk)} 条）: {e}")
            skipped += len(chunk)
            time.sleep(1)
            continue
        for r, v in zip(chunk, vecs):
            db.execute(
                "UPDATE articles SET embedding=? WHERE id=?",
                (json.dumps(v), r["id"]),
            )
        db.commit()
        done += len(chunk)
        print(f"[embed] {done}/{total}")
        time.sleep(1)  # 批间限速
    return {"done": done, "skipped": skipped}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()
    db = get_db(DATA_DIR / "knowledge.db")
    try:
        client = LLMClient(load_config(DATA_DIR))
    except LLMError as e:
        # 未配置 LLM 时给一行明确指引退出，而非裸堆栈（Agent/双击场景友好）
        print(f"[embed] 无法启动：{e}")
        print("[embed] 配置方法：复制 config.example.json 为 data/config.json 并填写，或启动服务后在页面右上角齿轮配置")
        sys.exit(1)
    r = backfill(db, client, args.batch)
    print(f"[embed] 完成: {r}")


if __name__ == "__main__":
    main()
