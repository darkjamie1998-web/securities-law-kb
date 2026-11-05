# tests/test_search.py — 混合检索测试（内存库 + mock llm，不联网）
import json

import pytest

import app.search as search
from app.db import get_db, init_db
from app.search import hybrid_search, _rrf
from app.seg_index import rebuild_all


@pytest.mark.parametrize("raw, expected", [
    ("第1条", "第一条"),
    ("第101条", "第一百零一条"),
    ("第1001条", "第一千零一条"),
    ("第9999条", "第九千九百九十九条"),
    ("第十九条之一", "第十九条之一"),
    ("第19条之2", "第十九条之二"),
    ("第19条之十", "第十九条之十"),
    ("第19条之2（经修改）", "第十九条之二"),
])
def test_normalize_article_no_preserves_compound_suffix(raw, expected):
    assert search.normalize_article_no(raw) == expected


@pytest.mark.parametrize("raw", [
    "第0条", "第10000条", "第19十一条", "第十九条之1一",
    "第一一十九条", "第十九条之零", "第十九条之三非法",
])
def test_normalize_article_no_does_not_guess_invalid_numbers(raw):
    assert search.normalize_article_no(raw) == raw


def test_parse_citation_rejects_out_of_range_without_crashing():
    assert search.parse_citation("证券法第10000条") is None
    raw = "第" + "9" * 5000 + "条"
    assert search.normalize_article_no(raw) == raw


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

    embedding_model = "mock-embed"  # hybrid_search 据此判断是否走向量路

    def embed(self, texts, batch=64):
        return [[1.0, 0.0] if "内幕" in t else [0.0, 1.0] for t in texts]


# ---- 短词兜底：trigram FTS 只支持 ≥3 字查询，"税"这类单字/双字走 LIKE ----

@pytest.fixture()
def tax_db(tmp_path):
    conn = get_db(tmp_path / "tax.db")
    init_db(conn)
    for i, (title, text) in enumerate([
        ("增值税法", "第一条 税收征收管理。第二条 税率设置。"),
        ("证券法", "第一条 证券发行。第二条 税务登记事项说明。"),
        ("公司法", "第一条 公司设立。第二条 股东权利。"),
    ]):
        cur = conn.execute(
            "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
            (title, "法律", f"h{i}"))
        conn.execute(
            "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
            (cur.lastrowid, "第一条", text))
    conn.commit()
    return conn


def test_single_char_query_hits_via_like(tax_db):
    """单字"税"：FTS 无法命中（trigram 需 ≥3 字），LIKE 兜底应命中含税条文。"""
    results = hybrid_search(tax_db, None, "税", top_k=10)
    titles = {r["title"] for r in results}
    assert "增值税法" in titles and "证券法" in titles
    assert "公司法" not in titles   # 不含税字的条文不应出现


def test_two_char_query_hits_via_like(tax_db):
    """双字"税收"同样走 LIKE 兜底。"""
    results = hybrid_search(tax_db, None, "税收", top_k=10)
    assert any("税收" in r["text"] for r in results)


def test_like_ranks_by_hit_count(tax_db):
    """LIKE 兜底按出现次数排序：税字出现更多的条文排前。"""
    results = hybrid_search(tax_db, None, "税", top_k=10)
    assert results[0]["title"] == "增值税法"  # 3 次税 > 证券法 1 次


def test_three_char_still_uses_fts(tax_db):
    """≥3 字查询仍走 FTS 主路（兜底不改变原有行为）。"""
    results = hybrid_search(tax_db, None, "公司设立", top_k=10)
    assert any("公司设立" in r["text"] for r in results)


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
        embedding_model = "mock-embed"

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


@pytest.fixture()
def paged_db(tmp_path):
    conn = get_db(tmp_path / "paged.db")
    init_db(conn)
    for i in range(6):
        cur = conn.execute(
            """INSERT INTO laws(title, level, status, source_name, topic_tags,
               content_hash) VALUES(?,?,?,?,?,?)""",
            (f"监管规定{i}", "法律" if i == 0 else "行政法规",
             "现行有效" if i == 0 else "已修订",
             "证监会" if i == 0 else "上交所",
             "发行， 上市" if i == 0 else "交易", f"page-{i}"),
        )
        conn.execute(
            "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
            (cur.lastrowid, "第一条", f"证券发行监管第{i}项规则，明确发行要求。"),
        )
    first_law_id = conn.execute(
        "SELECT id FROM laws WHERE title='监管规定0'").fetchone()[0]
    for title, flags in [
        ("重复监管规定", {"duplicate_of": first_law_id, "quality_flag": "duplicate"}),
        ("空监管规定", {"quality_flag": "empty_text"}),
        ("旧版监管规定", {"superseded_by": first_law_id, "status": "已修订"}),
    ]:
        cur = conn.execute(
            """INSERT INTO laws(title, level, content_hash, duplicate_of,
               superseded_by, quality_flag, status) VALUES(?,?,?,?,?,?,?)""",
            (title, "法律", f"excluded-{title}", flags.get("duplicate_of"),
             flags.get("superseded_by"), flags.get("quality_flag"),
             flags.get("status", "现行有效")),
        )
        conn.execute(
            "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
            (cur.lastrowid, "第一条", "证券发行监管规则，明确发行要求。"),
        )
    conn.commit()
    rebuild_all(conn)
    return conn


def test_search_page_reports_all_filtered_candidates_and_real_pages(paged_db):
    first = search.hybrid_search_page(
        paged_db, None, "证券发行监管", page=1, page_size=2)
    second = search.hybrid_search_page(
        paged_db, None, "证券发行监管", page=2, page_size=2)

    assert first["total"] == 6  # 重复、空数据与旧版候选不计入 total
    assert first["page"] == 1 and first["page_size"] == 2
    assert len(first["results"]) == 2 and first["has_more"] is True
    assert first["mode"] == "keyword"
    assert second["total"] == 6 and len(second["results"]) == 2
    assert {r["id"] for r in first["results"]}.isdisjoint(
        r["id"] for r in second["results"])

    empty = search.hybrid_search_page(
        paged_db, None, "证券发行监管", page=4, page_size=2)
    assert empty["total"] == 6
    assert empty["results"] == [] and empty["has_more"] is False


def test_search_page_combines_all_law_filters(paged_db):
    law_id = paged_db.execute(
        "SELECT id FROM laws WHERE title='监管规定0'").fetchone()[0]
    filters = {
        "level": "法律", "status": "现行有效", "source": "证监会",
        "topic": " 上市 ", "law_id": law_id,
    }
    matched = search.hybrid_search_page(
        paged_db, None, "证券发行监管", filters=filters)
    assert matched["total"] == 1
    assert [r["law_id"] for r in matched["results"]] == [law_id]

    for key, value in [
        ("level", "行政法规"), ("status", "已修订"),
        ("source", "证监会北京监管局"), ("topic", "上市委"),
        ("law_id", law_id + 999),
    ]:
        mismatched = dict(filters)
        mismatched[key] = value
        assert search.hybrid_search_page(
            paged_db, None, "证券发行监管", filters=mismatched)["total"] == 0

    blank_topic = dict(filters, topic=" \t\n\r　")
    assert search.hybrid_search_page(
        paged_db, None, "证券发行监管", filters=blank_topic)["total"] == 0


def test_exact_article_uses_same_filters_and_stays_first(db):
    law_id = db.execute(
        "SELECT id FROM laws WHERE title='中华人民共和国证券法'").fetchone()[0]
    db.execute(
        "UPDATE laws SET source_name='证监会', topic_tags='发行，上市' WHERE id=?",
        (law_id,))
    db.commit()
    filters = {
        "level": "法律", "status": "现行有效", "source": "证监会",
        "topic": "上市", "law_id": law_id,
    }
    page = search.hybrid_search_page(
        db, None, "证券法第五十条", filters=filters)
    assert page["total"] >= 1
    assert page["results"][0]["article_no"] == "第五十条"
    assert page["results"][0]["match_type"] == "exact_article"
    assert page["results"][0]["match_reasons"][0] == "exact_article"

    for key, value in [
        ("level", "行政法规"), ("status", "已修订"),
        ("source", "上交所"), ("topic", "交易"), ("law_id", law_id + 999),
    ]:
        mismatched = dict(filters)
        mismatched[key] = value
        assert search.hybrid_search_page(
            db, None, "证券法第五十条", filters=mismatched)["total"] == 0


def test_snippet_centers_earliest_real_hit_and_lists_highlight_terms(db):
    cur = db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (db.execute("SELECT id FROM laws").fetchone()[0], "第二百条",
         "前" * 300 + "内幕交易" + "后" * 300),
    )
    db.commit()
    rebuild_all(db)
    article_id = cur.lastrowid

    page = search.hybrid_search_page(db, None, "内幕交易", page_size=10)
    result = next(r for r in page["results"] if r["id"] == article_id)
    assert len(result["snippet"]) <= 260
    assert 80 <= result["snippet"].index("内幕交易") <= 150
    assert "内幕交易" in result["highlight_terms"]
    assert len(result["highlight_terms"]) == len(set(result["highlight_terms"]))
    assert result["highlight_terms"] == sorted(
        result["highlight_terms"], key=len, reverse=True)
    assert {"segment", "keyword"}.issubset(result["match_reasons"])


def test_page_reports_vector_reason_and_hybrid_mode_only_when_used(db):
    page = search.hybrid_search_page(
        db, MockLLM(), "完全无关的查询词xyz", page_size=5)
    assert page["mode"] == "hybrid"
    assert page["results"]
    assert all(r["match_reasons"] == ["vector"] for r in page["results"])

    class BrokenLLM:
        embedding_model = "mock-embed"

        def embed(self, texts, batch=64):
            raise RuntimeError("no api")

    degraded = search.hybrid_search_page(
        db, BrokenLLM(), "证券发行", page_size=5)
    assert degraded["mode"] == "keyword"
    assert all("vector" not in r["match_reasons"] for r in degraded["results"])


def test_legacy_search_wraps_first_page_and_defends_bad_paging(db):
    paged = search.hybrid_search_page(
        db, None, "内幕交易", page=1, page_size=1,
        filters={"level": "法律"})
    assert hybrid_search(
        db, None, "内幕交易", top_k=1, filters={"level": "法律"}
    ) == paged["results"]

    defended = search.hybrid_search_page(
        db, None, "内幕交易", page=0, page_size=0)
    assert defended["page"] == 1 and defended["page_size"] == 1
    assert len(defended["results"]) == 1


@pytest.fixture()
def vector_page_db(tmp_path):
    """高 ID 的排除项占据向量前列，top50 截断会错误缩小有效 total。"""
    conn = get_db(tmp_path / "vector-page.db")
    init_db(conn)
    first_law_id = None
    for i in range(67):  # 55 部有效 + 6 部重复 + 6 部旧版
        duplicate_of = first_law_id if 55 <= i < 61 else None
        superseded_by = first_law_id if i >= 61 else None
        title = (f"向量法规{i}" if i < 55 else
                 f"重复向量法规{i}" if i < 61 else f"旧版向量法规{i}")
        cur = conn.execute(
            """INSERT INTO laws(title, level, content_hash, duplicate_of,
               superseded_by, quality_flag) VALUES(?,?,?,?,?,?)""",
            (title, "法律", f"vector-{i}", duplicate_of, superseded_by,
             "duplicate" if duplicate_of else None),
        )
        if i == 0:
            first_law_id = cur.lastrowid
        conn.execute(
            "INSERT INTO articles(law_id, article_no, text, embedding) "
            "VALUES(?,?,?,?)",
            (cur.lastrowid, "第一条", f"向量候选正文{i}", json.dumps([1.0, 0.0])),
        )
    conn.commit()
    return conn


def test_vector_page_total_is_not_truncated_by_excluded_top_candidates(vector_page_db):
    page = search.hybrid_search_page(
        vector_page_db, MockLLM(), "内幕", page=1, page_size=10)
    assert page["mode"] == "hybrid"
    assert page["total"] == 55
    assert len(page["results"]) == 10 and page["has_more"] is True

    last = search.hybrid_search_page(
        vector_page_db, MockLLM(), "内幕", page=6, page_size=10)
    assert last["total"] == 55
    assert len(last["results"]) == 5 and last["has_more"] is False
    assert all(r["match_reasons"] == ["vector"] for r in last["results"])


def test_search_page_loads_text_only_for_current_page(vector_page_db):
    statements = []
    vector_page_db.set_trace_callback(statements.append)
    page = search.hybrid_search_page(
        vector_page_db, MockLLM(), "内幕", page=2, page_size=3)
    vector_page_db.set_trace_callback(None)

    assert page["total"] == 55 and len(page["results"]) == 3
    candidate_queries = [
        " ".join(sql.split()) for sql in statements
        if "FROM articles a JOIN laws l" in sql
    ]
    assert len(candidate_queries) == 2
    filter_query, text_query = candidate_queries
    assert "a.text" not in filter_query
    assert "a.text" in text_query
    expected_ids = ",".join(str(r["id"]) for r in page["results"])
    assert f"a.id IN ({expected_ids})" in text_query  # trace 会展开绑定参数
