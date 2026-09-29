# tests/test_audit.py — 图谱完整性审计测试
import pytest

from app.audit import audit, fix_ghost_marks
from app.db import get_db, init_db


@pytest.fixture()
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    return conn


def _add_law(db, i, title):
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        (title, "法律", f"h{i}"))
    db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (cur.lastrowid, "第一条", f"{title} 内容"))
    db.commit()
    return cur.lastrowid


def _add_rel(db, from_art, to_art, rel_type="引用"):
    db.execute(
        "INSERT INTO relations(from_id, to_id, rel_type, note) VALUES(?,?,?,?)",
        (from_art, to_art, rel_type, "n"))
    db.commit()


def test_audit_clean_db(db):
    r = audit(db)
    assert r["total_laws"] == 0 and r["total_edges"] == 0
    assert r["redundant"] == [] and r["isolated"] == [] and r["ghost"] == []


def test_audit_detects_redundant_and_isolated(db):
    a1, a2, a3 = _add_law(db, 1, "法A"), _add_law(db, 2, "法B"), _add_law(db, 3, "法C")
    _add_rel(db, a1, a2)
    _add_rel(db, a2, a1)   # 对称冗余
    # 法C 无任何关系 → 孤立
    r = audit(db)
    assert len(r["redundant"]) == 1
    assert [x["id"] for x in r["isolated"]] == [a3]
    assert r["ghost"] == []


def test_audit_detects_and_fixes_ghost_marks(db):
    a1, a2 = _add_law(db, 1, "法A"), _add_law(db, 2, "法B")
    _add_rel(db, a1, a2)
    db.execute("INSERT INTO relation_extractions(law_id, status, saved) VALUES(?, 'ok', 1)", (a1,))
    db.commit()
    assert audit(db)["ghost"] == []
    # 模拟历史 bug：A 的出边被删但标记残留
    db.execute("DELETE FROM relations")
    db.commit()
    r = audit(db)
    assert len(r["ghost"]) == 1 and r["ghost"][0]["law_id"] == a1
    # fix 清除标记
    assert fix_ghost_marks(db) == 1
    assert audit(db)["ghost"] == []
    assert db.execute("SELECT count(*) FROM relation_extractions").fetchone()[0] == 0


def test_fix_ghost_keeps_healthy_marks(db):
    a1, a2 = _add_law(db, 1, "法A"), _add_law(db, 2, "法B")
    _add_rel(db, a1, a2)
    db.execute("INSERT INTO relation_extractions(law_id, status, saved) VALUES(?, 'ok', 1)", (a1,))
    db.commit()
    assert fix_ghost_marks(db) == 0  # 边还在，标记健康
    assert db.execute("SELECT count(*) FROM relation_extractions").fetchone()[0] == 1
