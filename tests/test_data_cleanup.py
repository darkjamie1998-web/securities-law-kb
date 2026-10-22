"""数据治理 CLI 测试：去重标记、质量标记。"""
import os

from app.db import get_db, init_db
from tools.data_cleanup import (
    normalize_title, similarity, dedupe, mark_quality, prune_backups,
)


def _add(db, title, text, source="测试", hash_=None):
    cur = db.execute(
        "INSERT INTO laws(title, level, source_name, content_hash, full_text) "
        "VALUES(?,?,?,?,?)",
        (title, "法律", source, hash_ or title + text[:10], text))
    db.execute("INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
               (cur.lastrowid, "第一条", text))
    db.commit()
    return cur.lastrowid


def test_normalize_title():
    assert normalize_title("【证监会公告】《证券法》（2025年修订）") == "证券法"
    assert normalize_title("  中华人民共和国证券法  ") == "中华人民共和国证券法"


def test_similarity_threshold():
    assert similarity("第一条 内容。" * 100, "第一条 内容。" * 100) > 0.99


def test_dedupe_marks_duplicates(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    a = _add(db, "证券法", "第一条 内容。" * 100, source="国家法律法规数据库")
    b = _add(db, "【转载】证券法", "第一条 内容。" * 100, source="其他")
    assert dedupe(db, dry_run=False) >= 1
    b_row = db.execute("SELECT duplicate_of,quality_flag FROM laws WHERE id=?", (b,)).fetchone()
    assert b_row["duplicate_of"] == a
    assert b_row["quality_flag"] == "duplicate"
    db.close()


def test_mark_quality_empty_text(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    lid = _add(db, "空法规", "短")
    assert mark_quality(db, dry_run=False) >= 1
    assert db.execute("SELECT quality_flag FROM laws WHERE id=?", (lid,)).fetchone()[0] == "empty_text"
    db.close()


def test_mark_quality_marks_unsplit(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    cur = db.execute(
        "INSERT INTO laws(title, level, source_name, content_hash, full_text) "
        "VALUES('行业标准公告','规范性文件','测试','h9',?)",
        ("本公告没有条文结构，" * 30,))
    lid = cur.lastrowid
    db.execute("INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
               (lid, "", "本公告没有条文结构，" * 30))
    db.commit()
    mark_quality(db, dry_run=False)
    flag = db.execute("SELECT quality_flag FROM laws WHERE id=?", (lid,)).fetchone()[0]
    assert flag == "unsplit"
    db.close()


def test_prune_backups_keeps_latest(tmp_path):
    for i in range(4):
        p = tmp_path / f"knowledge-2026010{i}.db"
        p.write_bytes(b"x")
        os.utime(p, (i, i))
    removed = prune_backups(keep=2, dest_dir=tmp_path)
    assert len(removed) == 2
    assert len(list(tmp_path.glob("knowledge-*.db"))) == 2
