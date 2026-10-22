"""人工状态维护测试。"""
import pytest

from app.db import get_db, init_db
from tools.set_status import set_status


@pytest.fixture
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    for i, title in enumerate(["中华人民共和国证券法", "测试法"]):
        conn.execute(
            "INSERT INTO laws(title, level, content_hash, status) VALUES(?,?,?,?)",
            (title, "法律", f"h{i}", "现行有效"))
    conn.commit()
    yield conn
    conn.close()


def test_set_status_by_id(db):
    lid = db.execute("SELECT id FROM laws WHERE title='测试法'").fetchone()[0]
    changed = set_status(db, law_id=lid, status="已废止")
    assert changed[0]["old"] == "现行有效"
    assert db.execute("SELECT status FROM laws WHERE id=?", (lid,)).fetchone()[0] == "已废止"


def test_set_status_by_title(db):
    changed = set_status(db, title="证券法", status="已修订")
    assert "证券法" in changed[0]["title"]
    assert db.execute(
        "SELECT status FROM laws WHERE title='中华人民共和国证券法'"
    ).fetchone()[0] == "已修订"


def test_set_status_dry_run_does_not_write(db):
    lid = db.execute("SELECT id FROM laws WHERE title='测试法'").fetchone()[0]
    set_status(db, law_id=lid, status="已废止", dry_run=True)
    assert db.execute("SELECT status FROM laws WHERE id=?", (lid,)).fetchone()[0] == "现行有效"


def test_set_status_rejects_ambiguous_title(db):
    db.execute("INSERT INTO laws(title, level, content_hash) VALUES('测试法实施条例','法律','h3')")
    db.commit()
    with pytest.raises(ValueError):
        set_status(db, title="测试法", status="已废止")


def test_set_status_rejects_invalid_status(db):
    lid = db.execute("SELECT id FROM laws WHERE title='测试法'").fetchone()[0]
    with pytest.raises(ValueError):
        set_status(db, law_id=lid, status="未知状态")
