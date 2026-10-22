"""同步版本化测试：内容变化保留旧版快照，引用方标记失效。"""
from app.db import get_db, init_db
from app.seg_index import ensure_schema
from crawler.sync import upsert_law


def _source():
    return {"name": "测试来源", "level": "法律"}


def _doc(text):
    return {"title": "测试法", "full_text": text, "source_url": "http://x"}


def test_update_creates_superseded_snapshot(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    assert upsert_law(db, _source(), _doc("第一条 旧内容。" * 20)) == "new"
    assert upsert_law(db, _source(), _doc("第一条 新内容。" * 20)) == "updated"
    rows = db.execute("SELECT id,status,superseded_by FROM laws ORDER BY id").fetchall()
    assert len(rows) == 2
    current, snapshot = rows          # 当前版本保留原 id；旧版快照是新 id
    assert current["status"] == "现行有效"
    assert current["superseded_by"] is None
    assert snapshot["status"] == "已修订"
    assert snapshot["superseded_by"] == current["id"]
    old_arts = db.execute(
        "SELECT count(*) FROM articles WHERE law_id=?", (snapshot["id"],)).fetchone()[0]
    assert old_arts > 0
    db.close()


def test_same_content_is_skip(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    doc = _doc("第一条 内容。" * 30)
    assert upsert_law(db, _source(), doc) == "new"
    assert upsert_law(db, _source(), doc) == "skip"
    db.close()


def test_upsert_uses_source_status(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    doc = _doc("第一条 状态测试。" * 30)
    doc["status"] = "已废止"
    assert upsert_law(db, _source(), doc) == "new"
    assert db.execute("SELECT status FROM laws").fetchone()[0] == "已废止"
    db.close()


def test_update_marks_referencing_laws_pending(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    upsert_law(db, {"name": "S", "level": "法律"},
               {"title": "A法", "full_text": "第一条 A。" * 30})
    upsert_law(db, {"name": "S", "level": "法律"},
               {"title": "B法", "full_text": "第一条 B。" * 30})
    a = db.execute("SELECT id FROM laws WHERE title='A法'").fetchone()[0]
    b = db.execute("SELECT id FROM laws WHERE title='B法'").fetchone()[0]
    a_art = db.execute("SELECT id FROM articles WHERE law_id=?", (a,)).fetchone()[0]
    b_art = db.execute("SELECT id FROM articles WHERE law_id=?", (b,)).fetchone()[0]
    db.execute("INSERT INTO relations(from_id,to_id,rel_type) VALUES(?,?,?)",
               (b_art, a_art, "引用"))
    db.execute("INSERT INTO relation_extractions(law_id,status) VALUES(?, 'ok')", (b,))
    db.commit()
    upsert_law(db, {"name": "S", "level": "法律"},
               {"title": "A法", "full_text": "第一条 A 新。" * 30})
    assert db.execute(
        "SELECT count(*) FROM relation_extractions WHERE law_id=?", (b,)
    ).fetchone()[0] == 0
    db.close()
