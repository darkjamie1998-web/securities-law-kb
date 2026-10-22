# app/seg_index.py — jieba 分词 + FTS5 索引（BM25 中文检索）
# 分词索引是派生数据：可随时用 `python -m app.seg_index --rebuild` 重建。
import sqlite3
from pathlib import Path

import jieba

_DICT_LOADED = False


def _load_user_dict() -> None:
    global _DICT_LOADED
    if _DICT_LOADED:
        return
    path = Path(__file__).with_name("legal_terms.txt")
    if path.exists():
        jieba.load_userdict(str(path))
    _DICT_LOADED = True


def add_titles(titles) -> None:
    """把法规标题加入词典，提升法规名整体成词率。"""
    _load_user_dict()
    for title in titles:
        t = str(title).strip().strip("《》")
        if 2 <= len(t) <= 20:
            jieba.add_word(t, freq=10000)


def seg_text(text: str) -> str:
    _load_user_dict()
    return " ".join(w for w in jieba.cut_for_search(text or "") if w.strip())


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS articles_seg USING fts5(
               seg, article_no, tokenize='unicode61')"""
    )
    db.commit()


def seg_count(db: sqlite3.Connection) -> int:
    return db.execute("SELECT count(*) FROM articles_seg").fetchone()[0]


def reindex_law(db: sqlite3.Connection, law_id: int) -> int:
    """重建一部法规的全部分词索引（sync 更新后调用）。"""
    ensure_schema(db)
    ids = [r[0] for r in db.execute(
        "SELECT id FROM articles WHERE law_id=?", (law_id,))]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM articles_seg WHERE rowid IN ({placeholders})", ids)
    rows = db.execute(
        f"SELECT id, text, article_no FROM articles WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    for r in rows:
        db.execute(
            "INSERT INTO articles_seg(rowid, seg, article_no) VALUES(?,?,?)",
            (r["id"], seg_text(r["text"]), r["article_no"] or ""),
        )
    return len(rows)


def rebuild_all(db: sqlite3.Connection) -> int:
    ensure_schema(db)
    db.execute("DELETE FROM articles_seg")
    # 只索引现行且非重复法规：旧版快照与重复项不参与检索
    rows = db.execute(
        """SELECT a.id, a.text, a.article_no FROM articles a
           JOIN laws l ON l.id = a.law_id
           WHERE l.duplicate_of IS NULL AND l.superseded_by IS NULL"""
    ).fetchall()
    for r in rows:
        db.execute(
            "INSERT INTO articles_seg(rowid, seg, article_no) VALUES(?,?,?)",
            (r["id"], seg_text(r["text"]), r["article_no"] or ""),
        )
    db.commit()
    return len(rows)


def ensure_fresh(db: sqlite3.Connection) -> int:
    """补齐缺失的分词索引行，返回补齐数量。"""
    ensure_schema(db)
    missing = db.execute(
        """SELECT a.id, a.text, a.article_no FROM articles a
           JOIN laws l ON l.id = a.law_id
           WHERE l.duplicate_of IS NULL AND l.superseded_by IS NULL
             AND NOT EXISTS (SELECT 1 FROM articles_seg s WHERE s.rowid = a.id)"""
    ).fetchall()
    for r in missing:
        db.execute(
            "INSERT INTO articles_seg(rowid, seg, article_no) VALUES(?,?,?)",
            (r["id"], seg_text(r["text"]), r["article_no"] or ""),
        )
    if missing:
        db.commit()
    return len(missing)


if __name__ == "__main__":
    import argparse

    from app.db import get_db

    ap = argparse.ArgumentParser(description="jieba 分词索引维护")
    ap.add_argument("--rebuild", action="store_true", help="全量重建")
    ap.add_argument("--ensure", action="store_true", help="补齐缺失行")
    args = ap.parse_args()
    db = get_db(Path(__file__).resolve().parent.parent / "data" / "knowledge.db")
    if args.rebuild:
        print("rebuilt", rebuild_all(db))
    else:
        print("filled", ensure_fresh(db))
