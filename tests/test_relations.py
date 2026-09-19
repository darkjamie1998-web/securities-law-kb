# tests/test_relations.py — 标题模糊定位与关系入库测试（不调 LLM）
import pytest

from app.db import get_db, init_db
from app.relations import _match_law, _first_article, REL_TYPES


@pytest.fixture()
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    for i, (title, h) in enumerate([
        ("中华人民共和国证券法", "h1"),
        ("中华人民共和国公司法（2023年修订）", "h2"),
        ("深圳证券交易所股票上市规则", "h3"),
    ]):
        cur = conn.execute(
            "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
            (title, "法律", h),
        )
        conn.execute(
            "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
            (cur.lastrowid, "第一条", f"{title} 测试条文"),
        )
    conn.commit()
    return conn


def test_match_exact(db):
    row = _match_law(db, "中华人民共和国证券法")
    assert row and row["title"] == "中华人民共和国证券法"


def test_match_strips_version_suffix(db):
    row = _match_law(db, "中华人民共和国公司法（2018年修正）")
    assert row and row["title"].startswith("中华人民共和国公司法")


def test_match_fuzzy_contains(db):
    row = _match_law(db, "股票上市规则")
    assert row and row["title"] == "深圳证券交易所股票上市规则"


def test_match_none_for_unknown(db):
    assert _match_law(db, "量子计算机管理条例") is None


def test_first_article(db):
    row = _first_article(db, 1)
    assert row and row["id"] == 1


def test_rel_types_complete():
    assert set(REL_TYPES) == {"引用", "修订替代", "上位法", "同一事项", "程序衔接"}
