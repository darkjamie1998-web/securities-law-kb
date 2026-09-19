# tests/test_search.py — 混合检索测试（内存库 + mock llm，不联网）
import json

import pytest

from app.db import get_db, init_db
from app.search import hybrid_search, _rrf


@pytest.fixture()
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("中华人民共和国证券法", "法律", "h1"),
    )
    law_id = cur.lastrowid
    texts = [
        ("第一条", "为了规范证券发行和交易行为，保护投资者的合法权益，制定本法。"),
        ("第五十条", "禁止证券交易内幕信息的知情人和非法获取内幕信息的人利用内幕信息从事证券交易活动。"),
        ("第一百九十一条", "证券交易内幕信息的知情人或者非法获取内幕信息的人，违反本法规定从事内幕交易的，责令依法处理非法持有的证券。"),
    ]
    for no, t in texts:
        cur2 = conn.execute(
            "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
            (law_id, no, t),
        )
        # 预置 mock 向量：含"内幕"的条文为 [1,0]，其余 [0,1]（与 MockLLM 对齐）
        vec = [1.0, 0.0] if "内幕" in t else [0.0, 1.0]
        conn.execute(
            "UPDATE articles SET embedding=? WHERE id=?",
            (json.dumps(vec), cur2.lastrowid),
        )
    conn.commit()
    return conn


class MockLLM:
    """embed 返回简单向量：与含"内幕"的文本同向。"""

    def embed(self, texts, batch=64):
        return [[1.0, 0.0] if "内幕" in t else [0.0, 1.0] for t in texts]


def test_keyword_only_hits(db):
    results = hybrid_search(db, None, "内幕交易", top_k=5)
    assert results, "关键词路应有命中"
    assert any("内幕" in r["text"] for r in results)
    assert results[0]["title"] == "中华人民共和国证券法"


def test_hybrid_fusion(db):
    results = hybrid_search(db, MockLLM(), "利用内幕信息交易怎么处罚", top_k=5)
    assert results
    # 第191条同时命中两路，应排最前
    assert results[0]["article_no"] == "第一百九十一条"


def test_degrades_to_keyword_when_embed_fails(db):
    class BrokenLLM:
        def embed(self, texts, batch=64):
            raise RuntimeError("no api")

    results = hybrid_search(db, BrokenLLM(), "证券发行", top_k=5)
    assert results  # embedding 失败仍走关键词


def test_no_match_returns_empty(db):
    assert hybrid_search(db, None, "量子计算机检修规范", top_k=5) == []


def test_multi_word_query_hits(db):
    """多词查询（Agent 实际检索形态）：分词 OR 后应命中，而不是整串短语落空。"""
    results = hybrid_search(db, None, "内幕交易 处罚 规定", top_k=5)
    assert results, "分词多路匹配应有命中"
    assert any("内幕" in r["text"] for r in results)


def test_multi_word_query_with_unrelated_words(db):
    """包含无命中词的多词查询：有词命中即返回结果。"""
    results = hybrid_search(db, None, "内幕信息 量子力学", top_k=5)
    assert results


def test_rrf_fusion_priority():
    a = [1, 2, 3]
    b = [2, 4]
    fused = _rrf([a, b], top_k=5)
    # id=2 两路均出现，分数最高
    assert fused[0][0] == 2
    ids = [i for i, _ in fused]
    assert set(ids) == {1, 2, 3, 4}


def test_vector_road_used_when_embedding_stored(db):
    # 给第五十条写入 mock 向量 [1,0]
    row = db.execute(
        "SELECT id FROM articles WHERE article_no='第五十条'"
    ).fetchone()
    db.execute(
        "UPDATE articles SET embedding=? WHERE id=?",
        (json.dumps([1.0, 0.0]), row["id"]),
    )
    db.commit()
    results = hybrid_search(db, MockLLM(), "完全无关的查询词xyz", top_k=5)
    ids = [r["article_no"] for r in results]
    assert "第五十条" in ids  # 向量路命中
