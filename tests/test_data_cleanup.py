"""数据治理 CLI 测试：去重标记、质量标记。"""
from app.db import get_db, init_db
from tools.data_cleanup import normalize_title, similarity, dedupe, mark_quality


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
