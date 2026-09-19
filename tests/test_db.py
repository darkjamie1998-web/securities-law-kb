# tests/test_db.py
import pytest
from app.db import get_db, init_db


@pytest.fixture()
def db(tmp_path):
    conn = get_db(tmp_path / "test.db")
    init_db(conn)
    return conn


def test_init_creates_all_tables(db):
    tables = {
        r["name"]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"laws", "articles", "relations", "wiki_pages", "sync_log"} <= tables


def test_insert_law_and_article(db):
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("中华人民共和国证券法", "法律", "abc123"),
    )
    law_id = cur.lastrowid
    db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第一条", "为了规范证券发行和交易行为，保护投资者的合法权益…"),
    )
    db.commit()
    row = db.execute("SELECT title FROM laws WHERE id=?", (law_id,)).fetchone()
    assert row["title"] == "中华人民共和国证券法"
    cnt = db.execute(
        "SELECT count(*) AS c FROM articles WHERE law_id=?", (law_id,)
    ).fetchone()
    assert cnt["c"] == 1


def test_fts_sync_on_insert_and_delete(db):
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("测试法规", "部门规章", "h1"),
    )
    law_id = cur.lastrowid
    a_cur = db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第二条", "内幕交易是指证券交易活动中知悉内幕信息的人利用内幕信息从事证券交易的活动"),
    )
    db.commit()
    hits = db.execute(
        "SELECT article_no FROM articles_fts WHERE articles_fts MATCH '内幕交易'"
    ).fetchall()
    assert len(hits) == 1 and hits[0]["article_no"] == "第二条"

    db.execute("DELETE FROM articles WHERE id=?", (a_cur.lastrowid,))
    db.commit()
    hits = db.execute(
        "SELECT article_no FROM articles_fts WHERE articles_fts MATCH '内幕交易'"
    ).fetchall()
    assert len(hits) == 0


def test_content_hash_unique(db):
    db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("法规A", "法律", "dup"),
    )
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
            ("法规B", "法律", "dup"),
        )
