# tests/test_sync.py — upsert_law 数据一致性测试（更新带关系/缓存/wiki 的法规）
import pytest

from app.db import get_db, init_db
from crawler.sync import upsert_law

SOURCE = {"name": "测试来源", "level": "法律"}


@pytest.fixture()
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    return conn


def _doc(text, **kw):
    d = {"title": "中华人民共和国测试法", "full_text": text}
    d.update(kw)
    return d


def _seed_two_laws_with_relation(db):
    """入库两部法规并建立 A→B 关系、A 的抽取标记与 wiki 缓存。"""
    assert upsert_law(db, SOURCE, _doc("第一条 原文内容。" * 10)) == "new"
    assert upsert_law(db, SOURCE,
                      _doc("另一部法规全文。" * 10, title="另一部法规")) == "new"
    a_art = db.execute("SELECT id FROM articles WHERE law_id=1").fetchone()["id"]
    b_art = db.execute("SELECT id FROM articles WHERE law_id=2").fetchone()["id"]
    db.execute(
        "INSERT INTO relations(from_id, to_id, rel_type, note) VALUES(?,?,?,?)",
        (a_art, b_art, "引用", "测试关系"),
    )
    db.execute(
        "INSERT INTO relation_extractions(law_id, status) VALUES(1, 'ok')")
    db.execute(
        "INSERT INTO wiki_pages(law_id, markdown) VALUES(1, '旧解读')")
    db.commit()


def test_upsert_new_then_skip_unchanged(db):
    assert upsert_law(db, SOURCE, _doc("第一条 原文内容。" * 10)) == "new"
    assert upsert_law(db, SOURCE, _doc("第一条 原文内容。" * 10)) == "skip"


def test_update_clears_derived_data(db):
    """内容变更重爬：relations/抽取标记/wiki 缓存必须一并失效（FK 也会阻断）。"""
    _seed_two_laws_with_relation(db)
    assert upsert_law(db, SOURCE, _doc("第二条 变更后的内容。" * 10)) == "updated"
    assert db.execute("SELECT count(*) FROM relations").fetchone()[0] == 0
    assert db.execute(
        "SELECT count(*) FROM relation_extractions").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM wiki_pages").fetchone()[0] == 0
    # laws 与 articles 一致：新全文已生效，法条已重建
    assert "变更后的内容" in db.execute(
        "SELECT full_text FROM laws WHERE id=1").fetchone()["full_text"]
    texts = db.execute(
        "SELECT text FROM articles WHERE law_id=1").fetchall()
    assert texts and all("变更后的内容" in t["text"] for t in texts)


def test_update_rebuilds_fts(db):
    """更新后 FTS 索引与新法条一致（旧关键词不再命中）。"""
    _seed_two_laws_with_relation(db)
    upsert_law(db, SOURCE, _doc("第二条 全新关键词甲乙丙。" * 10))
    hit_old = db.execute(
        "SELECT count(*) FROM articles_fts WHERE articles_fts MATCH '原文内容'").fetchone()[0]
    hit_new = db.execute(
        "SELECT count(*) FROM articles_fts WHERE articles_fts MATCH '关键词甲乙丙'").fetchone()[0]
    assert hit_old == 0 and hit_new > 0


def test_update_invalidates_referencing_law_marks(db):
    """更新法规 B 时，指向 B 的引用方 A 的抽取标记必须连带失效（否则入向边永久丢失）。

    场景：A→B 有关系且 A 已标记 ok；B 内容变更重爬 →
    A 的标记应被清除（回到 pending，待重抽恢复 A→B 边），B 自身标记同样清除。
    """
    from app.relations import pending_laws
    _seed_two_laws_with_relation(db)  # law 1 → law 2 有关系，law 1 标记 ok
    assert upsert_law(db, SOURCE, _doc("变更内容。" * 15, title="另一部法规")) == "updated"
    # 双方标记都应被清（引用方 1 与被更新方 2）
    left = db.execute("SELECT law_id FROM relation_extractions").fetchall()
    assert [r["law_id"] for r in left] == []
    # 引用方回到 pending，将重新抽取以恢复入向边
    pend_ids = [p["id"] for p in pending_laws(db, 10)]
    assert 1 in pend_ids and 2 in pend_ids


def test_update_third_party_marks_kept(db):
    """更新法规 B 不应影响无关法规 C 的抽取标记（只连带直接引用方）。"""
    from app.relations import pending_laws
    _seed_two_laws_with_relation(db)
    upsert_law(db, SOURCE, _doc("第三部法规全文。" * 12, title="第三部法规"))  # law 3
    db.execute("INSERT INTO relation_extractions(law_id, status) VALUES(3, 'ok')")
    db.commit()
    upsert_law(db, SOURCE, _doc("再变更。" * 15, title="另一部法规"))  # 更新 law 2
    # law 1（引用方）标记被清；law 3（无关方）标记保留
    row3 = db.execute(
        "SELECT status FROM relation_extractions WHERE law_id=3").fetchone()
    assert row3 and row3["status"] == "ok"
    pend_ids = [p["id"] for p in pending_laws(db, 10)]
    assert 1 in pend_ids and 3 not in pend_ids
