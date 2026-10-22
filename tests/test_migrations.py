"""schema 迁移测试：v0 旧库升级到当前版本，幂等且不丢数据。"""
from app.db import get_db, init_db, SCHEMA
from app.migrations import CURRENT_VERSION


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migrate_v0_to_v1_adds_columns_and_indexes(tmp_path):
    db = get_db(tmp_path / "old.db")
    db.executescript(SCHEMA)          # 模拟旧库：只有基础表
    db.execute("PRAGMA user_version = 0")
    db.commit()
    init_db(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    cols = _columns(db, "laws")
    for name in ("quality_flag", "duplicate_of", "superseded_by", "version_label"):
        assert name in cols
    idx = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_relations_to" in idx
    assert "idx_laws_title" in idx
    db.close()


def test_migrate_is_idempotent(tmp_path):
    db = get_db(tmp_path / "old.db")
    db.executescript(SCHEMA)
    init_db(db)
    init_db(db)                        # 第二次不报错、不重复加列
    assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    db.close()


def test_migrate_keeps_existing_rows(tmp_path):
    db = get_db(tmp_path / "old.db")
    db.executescript(SCHEMA)
    db.execute("INSERT INTO laws(title, level) VALUES('证券法','法律')")
    db.commit()
    init_db(db)
    row = db.execute("SELECT title, status FROM laws").fetchone()
    assert row["title"] == "证券法"
    assert row["status"] == "现行有效"
    db.close()
