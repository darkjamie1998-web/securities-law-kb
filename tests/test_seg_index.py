"""jieba 分词索引测试：建表、单法规重建、全量重建、缺失补齐。"""
from app.db import get_db, init_db
from app.seg_index import (
    ensure_schema, reindex_law, rebuild_all, ensure_fresh, seg_count,
)


def _seed(db):
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES('中华人民共和国证券法','法律','h1')")
    law_id = cur.lastrowid
    for no, text in [
        ("第五十条", "禁止证券交易内幕信息的知情人和非法获取内幕信息的人利用内幕信息从事证券交易活动。"),
        ("第一百九十一条", "证券交易内幕信息的知情人或者非法获取内幕信息的人违反本法第五十三条的规定从事内幕交易的，责令依法处理非法持有的证券。"),
    ]:
        db.execute("INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
                   (law_id, no, text))
    db.commit()
    return law_id


def test_reindex_law_indexes_articles(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    law_id = _seed(db)
    reindex_law(db, law_id)
    assert seg_count(db) == 2
    rows = db.execute(
        "SELECT rowid FROM articles_seg WHERE articles_seg MATCH '内幕'").fetchall()
    assert len(rows) == 2
    db.close()


def test_reindex_law_is_idempotent(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    law_id = _seed(db)
    reindex_law(db, law_id)
    reindex_law(db, law_id)
    assert seg_count(db) == 2
    db.close()


def test_rebuild_all_and_ensure_fresh(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    _seed(db)
    assert rebuild_all(db) == 2
    assert seg_count(db) == 2
    # 删除一条索引后 ensure_fresh 补齐
    db.execute("DELETE FROM articles_seg WHERE rowid = (SELECT min(id) FROM articles)")
    db.commit()
    assert ensure_fresh(db) == 1
    assert seg_count(db) == 2
    db.close()
