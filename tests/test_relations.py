# tests/test_relations.py — 标题模糊定位与关系入库测试（不调 LLM）
import pytest

from app.db import get_db, init_db
from app.relations import (_match_law, _first_article, process_law,
                           pending_laws, REL_TYPES)


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


# ---- 抽取标记表：防止"无关系法规"被 forever runner 无限重试 ----

class _NoLLM:
    """process_law 的 llm 占位（monkeypatch 掉 extract_relations 后不会被真正调用）。"""


def test_process_law_marks_empty(db, monkeypatch):
    """抽取结果为空的法规也写 empty 标记，pending 查询不再选中它。"""
    from app import relations as R
    monkeypatch.setattr(R, "extract_relations", lambda *a, **k: [])
    result = process_law(db, _NoLLM(), 1)
    assert result["saved"] == 0
    row = db.execute(
        "SELECT status FROM relation_extractions WHERE law_id=1").fetchone()
    assert row and row["status"] == "empty"
    assert all(p["id"] != 1 for p in pending_laws(db, 10))


def test_process_law_marks_ok(db, monkeypatch):
    """正常抽取（含 unmatched）写 ok 标记。"""
    from app import relations as R
    monkeypatch.setattr(R, "extract_relations", lambda *a, **k: [
        {"ref_title": "中华人民共和国证券法", "rel_type": "引用", "note": "n"},
        {"ref_title": "量子计算机管理条例", "rel_type": "引用", "note": "n"},
    ])
    result = process_law(db, _NoLLM(), 2)
    assert result["saved"] == 1 and result["unmatched"] == 1
    row = db.execute(
        "SELECT status, extracted FROM relation_extractions WHERE law_id=2").fetchone()
    assert row["status"] == "ok" and row["extracted"] == 2


def test_failed_extraction_marks_and_retry_cap(db, monkeypatch):
    """LLM 异常写 failed 标记：attempts<上限时保持 pending，达上限后不再重试。"""
    from app import relations as R

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("网关内容安全过滤拦截")

    monkeypatch.setattr(R, "extract_relations", boom)
    for _ in range(R.MAX_ATTEMPTS + 1):
        with pytest.raises(RuntimeError):
            process_law(db, _NoLLM(), 3)
    row = db.execute(
        "SELECT status, attempts FROM relation_extractions WHERE law_id=3").fetchone()
    assert row["status"] == "failed" and row["attempts"] == R.MAX_ATTEMPTS + 1
    # 达到重试上限 → 不再 pending
    assert all(p["id"] != 3 for p in pending_laws(db, 10))


def test_failed_below_cap_stays_pending(db, monkeypatch):
    """failed 但未达上限的法规仍会重试（瞬时故障可恢复）。"""
    from app import relations as R

    def boom(*a, **k):
        raise RuntimeError("瞬时网络故障")

    monkeypatch.setattr(R, "extract_relations", boom)
    with pytest.raises(RuntimeError):
        process_law(db, _NoLLM(), 3)
    assert any(p["id"] == 3 for p in pending_laws(db, 10))


def test_pending_laws_untouched(db):
    """未处理过的法规都在 pending 中。"""
    ids = [p["id"] for p in pending_laws(db, 10)]
    assert ids == [1, 2, 3]
