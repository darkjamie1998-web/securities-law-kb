"""便携包构建测试：路径可移植、DB 快照可读。"""
import sqlite3

from build_portable import SRC, build_db_snapshot


def test_src_is_project_root():
    assert (SRC / "app" / "main.py").exists()
    assert (SRC / "build_portable.py").exists()


def test_db_snapshot_is_readable(tmp_path):
    src = tmp_path / "a.db"
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES(1)")
    c.commit()
    c.close()
    out = build_db_snapshot(src, tmp_path / "out.db")
    assert out.exists()
    assert sqlite3.connect(out).execute("SELECT x FROM t").fetchone()[0] == 1
