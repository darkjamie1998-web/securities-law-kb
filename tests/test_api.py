"""API 冒烟测试：应用工厂 + 临时数据库，不触碰生产数据。"""
import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
import app.main as main_module
from app.db import get_db, init_db
from app.llm import LLMError
from app.main import create_app
from app.search import normalize_article_no
from app.seg_index import rebuild_all


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as c:
        yield c


def test_health_and_stats(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["laws"] == 0
    assert client.get("/api/stats").json()["articles"] == 0


def test_law_list_detail_404(client):
    assert client.get("/api/laws").json()["total"] == 0
    assert client.get("/api/laws/999").status_code == 404


def test_settings_requires_no_key_leak(client):
    body = client.get("/api/settings").json()
    assert "api_key" not in body
    assert "api_key_masked" in body


def test_search_empty_query_422(client):
    assert client.get("/api/search").status_code == 422


def test_list_search_returns_200(client):
    assert client.get("/api/laws?q=证券").status_code == 200


@pytest.fixture
def seeded_client(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conn = get_db(data / "knowledge.db")
    init_db(conn)
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES('中华人民共和国证券法','法律','h1')")
    law_id = cur.lastrowid
    conn.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第五十条", "禁止利用内幕信息从事证券交易活动。"))
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash, duplicate_of, quality_flag) "
        "VALUES('重复法规','法律','h2',?,'duplicate')", (law_id,))
    conn.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (cur.lastrowid, "第一条", "重复内容。"))
    conn.execute(
        "INSERT INTO laws(title, level, content_hash, quality_flag) "
        "VALUES('空法规','法律','h3','empty_text')")
    conn.commit()
    conn.close()
    app = create_app(data)
    with TestClient(app) as c:
        yield c


def test_list_search_hits_seg_index(seeded_client):
    r = seeded_client.get("/api/laws?q=内幕").json()
    assert r["total"] == 1
    assert r["items"][0]["title"] == "中华人民共和国证券法"


def test_list_search_without_seg_table_falls_back_to_title(seeded_client):
    conn = get_db(seeded_client.app.state.data_dir / "knowledge.db")
    conn.execute("DROP TABLE articles_seg")
    conn.commit()
    conn.close()

    by_title = seeded_client.get("/api/laws", params={"q": "证券法"}).json()
    assert by_title["total"] == 1
    assert by_title["items"][0]["title"] == "中华人民共和国证券法"
    assert seeded_client.get("/api/laws", params={"q": "内幕"}).json()["total"] == 0


def test_list_search_more_than_500_segment_matches_is_not_capped(client):
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    conn.executemany(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        [(f"批量法规{i:04d}", "法律", f"bulk-{i}") for i in range(520)],
    )
    law_ids = [row[0] for row in conn.execute("SELECT id FROM laws ORDER BY id")]
    conn.executemany(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        [(law_id, "第一条", "禁止利用内幕信息从事证券交易活动。")
         for law_id in law_ids],
    )
    conn.commit()
    rebuild_all(conn)
    conn.close()

    first = client.get("/api/laws", params={
        "q": "内幕", "page": 1, "page_size": 100,
    }).json()
    assert first["total"] == 520
    assert [item["id"] for item in first["items"]] == list(range(520, 420, -1))

    last = client.get("/api/laws", params={
        "q": "内幕", "page": 6, "page_size": 100,
    }).json()
    assert last["total"] == 520
    assert last["page"] == 6 and last["page_size"] == 100
    assert [item["id"] for item in last["items"]] == list(range(20, 0, -1))


def test_list_default_excludes_duplicate_and_empty(seeded_client):
    r = seeded_client.get("/api/laws").json()
    titles = {x["title"] for x in r["items"]}
    assert titles == {"中华人民共和国证券法"}


def _seed_law_catalog(client):
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    conn.executemany(
        "INSERT INTO laws(title, level, content_hash, source_name, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?)", [
            ("较早法规", "法律", "catalog-1", "证监会",
             "2026-01-01", "2026-05-01"),
            ("中间法规", "法律", "catalog-2", "证监会北京监管局",
             "2026-01-02", "2026-02-01"),
            ("较新法规", "行政法规", "catalog-3", "上交所",
             "2026-04-01", None),
            ("同时间法规A", "法律", "catalog-4", "司法局",
             "2026-03-01", None),
            ("同时间法规B", "法律", "catalog-5", "司法局",
             "2026-03-01", None),
        ])
    conn.commit()
    conn.close()


def test_list_filters_by_exact_source(client):
    _seed_law_catalog(client)

    r = client.get("/api/laws", params={"source": "证监会"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert [item["title"] for item in body["items"]] == ["较早法规"]
    assert client.get("/api/laws", params={"source": "不存在"}).json()["total"] == 0


def test_list_supports_id_and_coalesced_updated_sort(client):
    _seed_law_catalog(client)

    default_body = client.get("/api/laws").json()
    id_body = client.get("/api/laws", params={"sort": "id"}).json()
    updated_body = client.get("/api/laws", params={"sort": "updated"}).json()

    assert [item["id"] for item in default_body["items"]] == [5, 4, 3, 2, 1]
    assert [item["id"] for item in id_body["items"]] == [5, 4, 3, 2, 1]
    assert [item["id"] for item in updated_body["items"]] == [1, 3, 5, 4, 2]
    same_time_ids = [
        item["id"] for item in updated_body["items"]
        if item["title"].startswith("同时间法规")
    ]
    assert same_time_ids == [5, 4]
    assert updated_body["total"] == 5
    assert updated_body["page"] == 1
    assert updated_body["page_size"] == 20
    assert client.get("/api/laws", params={"sort": "title"}).status_code == 422


def test_list_filters_topic_by_exact_normalized_tag(client):
    _seed_law_catalog(client)
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    conn.executemany("UPDATE laws SET topic_tags = ? WHERE id = ?", [
        (" 发行 ， 上市 ", 1),
        ("上市委，交易", 2),
        ("交易, 上市 ", 3),
    ])
    conn.commit()
    conn.close()

    listed = client.get("/api/laws", params={"topic": "上市"}).json()
    assert listed["total"] == 2
    assert [item["id"] for item in listed["items"]] == [3, 1]
    traded = client.get("/api/laws", params={"topic": "交易"}).json()
    assert [item["id"] for item in traded["items"]] == [3, 2]


def test_topic_facets_and_list_share_whitespace_normalization(client):
    _seed_law_catalog(client)
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    conn.executemany("UPDATE laws SET topic_tags = ? WHERE id = ?", [
        (" \t发行 ，\n上市\r ", 1),
        ("上市委，交易", 2),
        ("交易,　上市\t", 3),
        ("信息披露　，\r发行", 4),
    ])
    conn.commit()
    conn.close()

    stats = client.get("/api/stats").json()
    facets = {item["name"]: item["count"] for item in stats["topics"]}
    expected = {
        "发行": (2, [4, 1]),
        "上市": (2, [3, 1]),
        "上市委": (1, [2]),
        "交易": (2, [3, 2]),
        "信息披露": (1, [4]),
    }
    assert set(facets) == set(expected)
    for name, (count, ids) in expected.items():
        listed = client.get("/api/laws", params={"topic": name}).json()
        assert listed["total"] == facets[name] == count
        assert [item["id"] for item in listed["items"]] == ids


def test_topic_filter_is_case_sensitive_and_rejects_empty_tag(client):
    _seed_law_catalog(client)
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    conn.executemany("UPDATE laws SET topic_tags = ? WHERE id = ?", [
        ("IPO,上市", 1),
        ("ipo，上市", 2),
    ])
    conn.commit()
    conn.close()

    facets = {
        item["name"]: item["count"]
        for item in client.get("/api/stats").json()["topics"]
    }
    assert facets["IPO"] == 1
    assert facets["ipo"] == 1
    for name, law_id in (("IPO", 1), ("ipo", 2)):
        listed = client.get("/api/laws", params={"topic": name}).json()
        assert listed["total"] == facets[name]
        assert [item["id"] for item in listed["items"]] == [law_id]

    for empty_topic in ("", " \t\n\r　"):
        listed = client.get("/api/laws", params={"topic": empty_topic}).json()
        assert listed["total"] == 0
        assert listed["items"] == []


def _seed_search_results(client):
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    for i in range(5):
        cur = conn.execute(
            """INSERT INTO laws(title, level, status, source_name, topic_tags,
               content_hash) VALUES(?,?,?,?,?,?)""",
            (f"检索规定{i}", "法律" if i == 0 else "行政法规",
             "现行有效" if i == 0 else "已修订",
             "证监会" if i == 0 else "上交所",
             "发行， 上市" if i == 0 else "交易", f"api-search-{i}"),
        )
        conn.execute(
            "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
            (cur.lastrowid, "第一条", f"证券发行监管第{i}项规则。"),
        )
    conn.commit()
    conn.close()


def test_search_api_paginates_with_real_total_and_empty_page(client):
    _seed_search_results(client)
    first = client.get("/api/search", params={
        "q": "证券发行监管", "page": 1, "page_size": 2,
    })
    assert first.status_code == 200
    body = first.json()
    assert body["query"] == "证券发行监管"
    assert body["count"] == len(body["results"]) == 2
    assert body["total"] == 5
    assert body["page"] == 1 and body["page_size"] == 2
    assert body["has_more"] is True and body["mode"] == "keyword"
    for key in ("snippet", "highlight_terms", "match_reasons"):
        assert key in body["results"][0]

    second = client.get("/api/search", params={
        "q": "证券发行监管", "page": 2, "page_size": 2,
    }).json()
    assert second["count"] == 2 and second["total"] == 5
    assert {r["id"] for r in body["results"]}.isdisjoint(
        r["id"] for r in second["results"])

    empty = client.get("/api/search", params={
        "q": "证券发行监管", "page": 4, "page_size": 2,
    }).json()
    assert empty["count"] == 0 and empty["results"] == []
    assert empty["total"] == 5 and empty["has_more"] is False


def test_search_api_top_k_maps_to_page_size(client):
    _seed_search_results(client)
    body = client.get("/api/search", params={
        "q": "证券发行监管", "top_k": 1, "page_size": 3,
    }).json()
    assert body["page_size"] == 1
    assert body["count"] == 1 and body["total"] == 5


def test_search_api_combines_public_filters(client):
    _seed_search_results(client)
    matched = client.get("/api/search", params={
        "q": "证券发行监管", "level": "法律", "status": "现行有效",
        "source": "证监会", "topic": " 上市 ",
    }).json()
    assert matched["total"] == 1
    assert [r["title"] for r in matched["results"]] == ["检索规定0"]

    for extra in [
        {"level": "行政法规"}, {"status": "已修订"},
        {"source": "证监会北京监管局"}, {"topic": "上市委"},
        {"topic": " \t\n\r　"},
    ]:
        params = {
            "q": "证券发行监管", "level": "法律", "status": "现行有效",
            "source": "证监会", "topic": "上市",
        }
        params.update(extra)
        assert client.get("/api/search", params=params).json()["total"] == 0


def test_search_api_validates_paging(client):
    for params in [
        {"q": "证券", "page": 0}, {"q": "证券", "page_size": 0},
        {"q": "证券", "page_size": 101}, {"q": "证券", "top_k": 0},
        {"q": "证券", "top_k": 51},
    ]:
        assert client.get("/api/search", params=params).status_code == 422


def _seed_reader_law(client):
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash, full_text) VALUES(?,?,?,?)",
        ("长篇证券法规", "法律", "reader-law", "完整法规正文"),
    )
    law_id = cur.lastrowid
    rows = []
    for i in range(1, 124):
        text = f"共同检索词，第{i}项监管内容。"
        if i == 7:
            text += "比例为100%。"
        elif i == 8:
            text += "含下划线_标记。"
        elif i == 9:
            text += "含反斜杠\\标记。"
        article_no = f"第{i}条" if i % 2 == 0 else normalize_article_no(f"第{i}条")
        rows.append((law_id, article_no, text))
    conn.executemany(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)", rows)
    conn.execute(
        "INSERT INTO wiki_pages(law_id, markdown, model_used, generated_at) "
        "VALUES(?,?,?,?)",
        (law_id, "法规解读", "test-model", "2026-11-03"),
    )
    conn.commit()
    conn.close()
    return law_id


def test_law_detail_legacy_response_remains_full(client):
    law_id = _seed_reader_law(client)

    body = client.get(f"/api/laws/{law_id}").json()

    assert set(body) == {"law", "articles", "wiki"}
    assert body["law"]["full_text"] == "完整法规正文"
    assert len(body["articles"]) == 123
    assert body["articles"][0]["text"].startswith("共同检索词")
    assert body["wiki"]["markdown"] == "法规解读"


def test_law_reader_paginates_with_complete_lightweight_index(client):
    law_id = _seed_reader_law(client)

    default_body = client.get(
        f"/api/laws/{law_id}", params={"mode": "reader"}).json()
    assert default_body["article_page"] == 1
    assert default_body["article_page_size"] == 50
    assert len(default_body["articles"]) == 50

    body = client.get(f"/api/laws/{law_id}", params={
        "mode": "reader", "page": 2, "page_size": 50,
    }).json()
    assert set(body) == {
        "law", "articles", "article_index", "article_total", "article_page",
        "article_page_size", "target_article_id", "wiki",
    }
    assert body["law"]["title"] == "长篇证券法规"
    assert "full_text" not in body["law"]
    assert body["article_total"] == 123
    assert body["article_page"] == 2
    assert body["article_page_size"] == 50
    assert body["target_article_id"] is None
    assert [a["id"] for a in body["articles"]] == list(range(51, 101))
    assert len(body["article_index"]) == 123
    assert all(set(a) == {"id", "article_no"} for a in body["article_index"])
    assert [a["article_no"] for a in body["article_index"]] == [
        f"第{i}条" if i % 2 == 0 else normalize_article_no(f"第{i}条")
        for i in range(1, 124)
    ]
    assert body["wiki"]["markdown"] == "法规解读"


@pytest.mark.parametrize("article_no,target_id,page", [
    ("第51条", 51, 2),
    ("第一百零一条", 101, 3),
])
def test_law_reader_article_no_selects_containing_page(
        client, article_no, target_id, page):
    law_id = _seed_reader_law(client)

    body = client.get(f"/api/laws/{law_id}", params={
        "mode": "reader", "article_no": article_no, "page_size": 50,
    }).json()

    assert body["target_article_id"] == target_id
    assert body["article_page"] == page
    assert target_id in [a["id"] for a in body["articles"]]


def test_law_reader_unknown_or_mixed_article_no_does_not_guess(client):
    law_id = _seed_reader_law(client)

    for article_no in ("第19十一条", "第九百九十九条"):
        body = client.get(f"/api/laws/{law_id}", params={
            "mode": "reader", "article_no": article_no, "page": 2,
        }).json()
        assert body["target_article_id"] is None
        assert body["article_page"] == 2
        assert [a["id"] for a in body["articles"]] == list(range(51, 101))


def _seed_numbered_articles(client, article_nos):
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("条号边界法规", "法律", "reader-numbering"),
    )
    law_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        [(law_id, no, f"条文{position}") for position, no in enumerate(article_nos)],
    )
    conn.commit()
    conn.close()
    return law_id


@pytest.mark.parametrize("article_nos,reference_nos,target_id", [
    (["第五十条（经修改）"], ["第五十条", "第50条"], 1),
    (["第五十条", "第五十条之一(经修改)"],
     ["第五十条之一", "第50条之1"], 2),
])
def test_chat_citations_target_reader_parenthetical_articles(
        client, article_nos, reference_nos, target_id):
    law_id = _seed_numbered_articles(client, article_nos)
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    for reference_no in reference_nos:
        citations = chat_module._extract_citations(
            conn, f"【条号边界法规·{reference_no}】")
        assert len(citations) == 1
        citation = citations[0]
        assert citation["article_id"] == target_id

        body = client.get(f"/api/laws/{law_id}", params={
            "mode": "reader", "article_no": citation["article_no"],
            "page_size": 1,
        }).json()
        assert body["target_article_id"] == target_id
        assert body["article_page"] == target_id
        assert [a["id"] for a in body["articles"]] == [target_id]
    conn.close()


def test_reader_rejects_invalid_parenthetical_article_numbers(client):
    law_id = _seed_numbered_articles(client, ["第五十条", "第五十条之一"])
    for article_no in (
        "第五十条（经修改", "第五十条经修改）", "第五十条（经修改）尾部",
        "第五十条之一(经修改", "第五十条之一(经修改)尾部",
        "第五十条之（经修改）", "第5十条（经修改）",
        "第五十条之1一（经修改）", "第五十条（经（修改））",
    ):
        body = client.get(f"/api/laws/{law_id}", params={
            "mode": "reader", "article_no": article_no,
        }).json()
        assert body["target_article_id"] is None


def test_reader_preserves_compound_article_suffix(client):
    law_id = _seed_numbered_articles(client, ["第一百一十条", "第一百一十条之一"])

    for url in (f"/api/laws/{law_id}", f"/api/laws/{law_id}/articles"):
        extra = {"mode": "reader"} if url == f"/api/laws/{law_id}" else {}
        params = {"article_no": "第110条之一", "page_size": 1}
        params.update(extra)
        body = client.get(url, params=params).json()
        assert body["target_article_id"] == 2
        assert body["article_page"] == 2
        assert [a["id"] for a in body["articles"]] == [2]

    base = client.get(f"/api/laws/{law_id}", params={
        "mode": "reader", "article_no": "第一百一十条", "page_size": 1,
    }).json()
    assert base["target_article_id"] == 1 and base["article_page"] == 1

    for incomplete in ("第110条之", "第110条之壹", "第110条之10001"):
        body = client.get(f"/api/laws/{law_id}", params={
            "mode": "reader", "article_no": incomplete, "page_size": 1,
        }).json()
        assert body["target_article_id"] is None


def test_reader_normalizes_arabic_database_article_numbers(client):
    law_id = _seed_numbered_articles(client, ["第358条"])

    for url in (f"/api/laws/{law_id}", f"/api/laws/{law_id}/articles"):
        extra = {"mode": "reader"} if url == f"/api/laws/{law_id}" else {}
        params = {"article_no": "第三百五十八条"}
        params.update(extra)
        body = client.get(url, params=params).json()
        assert body["target_article_id"] == 1
        assert body["article_page"] == 1

    mixed = client.get(f"/api/laws/{law_id}", params={
        "mode": "reader", "article_no": "第3五十八条",
    }).json()
    assert mixed["target_article_id"] is None


def test_reader_limits_arabic_article_numbers_to_supported_range(client):
    law_id = _seed_numbered_articles(client, ["第一条", "第9999条"])

    upper = client.get(f"/api/laws/{law_id}", params={
        "mode": "reader", "article_no": "第九千九百九十九条",
    }).json()
    assert upper["target_article_id"] == 2

    for article_no in ("第0条", "第10000条", "第10001条"):
        response = client.get(f"/api/laws/{law_id}", params={
            "mode": "reader", "article_no": article_no,
        })
        assert response.status_code == 200
        assert response.json()["target_article_id"] is None


def test_reader_long_arabic_article_number_does_not_error(client):
    law_id = _seed_numbered_articles(client, ["第一条"])
    article_no = "第" + "9" * 5000 + "条"

    response = client.get(f"/api/laws/{law_id}", params={
        "mode": "reader", "article_no": article_no,
    })

    assert response.status_code == 200
    assert response.json()["target_article_id"] is None


def test_reader_prefers_raw_exact_article_match_before_normalization(client):
    law_id = _seed_numbered_articles(client, ["第三百五十八条", "第358条"])

    for article_no, expected_id, expected_page in (
        ("第358条", 2, 2),
        ("第三百五十八条", 1, 1),
    ):
        body = client.get(f"/api/laws/{law_id}", params={
            "mode": "reader", "article_no": article_no, "page_size": 1,
        }).json()
        assert body["target_article_id"] == expected_id
        assert body["article_page"] == expected_page


def test_law_reader_supports_empty_unsplit_law(client):
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash, full_text, quality_flag) "
        "VALUES(?,?,?,?,?)",
        ("无条号全文法规", "法律", "reader-empty", "未拆分全文 Alpha Beta", "unsplit"),
    )
    law_id = cur.lastrowid
    conn.commit()
    conn.close()

    legacy = client.get(f"/api/laws/{law_id}").json()
    assert legacy["law"]["full_text"] == "未拆分全文 Alpha Beta"
    assert legacy["articles"] == []

    body = client.get(f"/api/laws/{law_id}", params={"mode": "reader"}).json()
    fallback = {"id": None, "article_no": "全文", "text": "未拆分全文 Alpha Beta"}
    assert "full_text" not in body["law"]
    assert body["articles"] == [fallback]
    assert body["article_index"] == [{"id": None, "article_no": "全文"}]
    assert body["article_total"] == 1 and body["article_page"] == 1
    assert body["target_article_id"] is None and body["wiki"] is None

    articles = client.get(f"/api/laws/{law_id}/articles").json()
    assert articles["articles"] == [fallback] and articles["article_total"] == 1
    for q in ("未拆分", "alpha", "BETA"):
        matched = client.get(f"/api/laws/{law_id}/articles", params={"q": q}).json()
        assert matched["articles"] == [fallback] and matched["article_total"] == 1
    unmatched = client.get(f"/api/laws/{law_id}/articles", params={"q": "不存在"}).json()
    assert unmatched["articles"] == [] and unmatched["article_total"] == 0
    assert unmatched["article_index"] == [{"id": None, "article_no": "全文"}]


def test_law_articles_filters_and_paginates_within_law(client):
    law_id = _seed_reader_law(client)
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES('其他法规','法律','other')")
    conn.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (cur.lastrowid, "第一条", "共同检索词，其他法规内容。"),
    )
    conn.commit()
    conn.close()

    body = client.get(f"/api/laws/{law_id}/articles", params={
        "q": "共同检索词", "page": 3, "page_size": 50,
    }).json()
    assert set(body) == {
        "articles", "article_index", "article_total", "article_page",
        "article_page_size", "target_article_id",
    }
    assert body["article_total"] == 123
    assert [a["id"] for a in body["articles"]] == list(range(101, 124))
    assert len(body["article_index"]) == 123

    by_no = client.get(f"/api/laws/{law_id}/articles", params={
        "q": "第五十九条",
    }).json()
    assert by_no["article_total"] == 1
    assert [a["id"] for a in by_no["articles"]] == [59]

    empty_q = client.get(f"/api/laws/{law_id}/articles", params={
        "q": "", "page_size": 2,
    }).json()
    assert empty_q["article_total"] == 123
    assert [a["id"] for a in empty_q["articles"]] == [1, 2]


def test_law_articles_escapes_like_wildcards(client):
    law_id = _seed_reader_law(client)

    for q, expected_id in (("%", 7), ("_", 8), ("\\", 9)):
        body = client.get(f"/api/laws/{law_id}/articles", params={"q": q}).json()
        assert body["article_total"] == 1
        assert [a["id"] for a in body["articles"]] == [expected_id]


def test_law_articles_article_no_location_ignores_q(client):
    law_id = _seed_reader_law(client)

    body = client.get(f"/api/laws/{law_id}/articles", params={
        "article_no": "第51条", "q": "绝不命中", "page_size": 50,
    }).json()

    assert body["target_article_id"] == 51
    assert body["article_page"] == 2 and body["article_total"] == 123
    assert [a["id"] for a in body["articles"]] == list(range(51, 101))


def test_reader_and_articles_return_empty_page_beyond_end(client):
    law_id = _seed_reader_law(client)

    for url, extra in (
        (f"/api/laws/{law_id}", {"mode": "reader"}),
        (f"/api/laws/{law_id}/articles", {}),
    ):
        params = {"page": 4, "page_size": 50}
        params.update(extra)
        body = client.get(url, params=params).json()
        assert body["articles"] == []
        assert body["article_total"] == 123
        assert len(body["article_index"]) == 123
        assert body["article_page"] == 4


def test_reader_and_articles_validate_paging_and_missing_law(client):
    law_id = _seed_reader_law(client)

    for legacy_params in (
        {"page_size": 101, "page": 0},
        {"page_size": "not-a-number", "page": "not-a-number"},
    ):
        legacy = client.get(f"/api/laws/{law_id}", params=legacy_params)
        assert legacy.status_code == 200
        assert len(legacy.json()["articles"]) == 123

    for url, extra in (
        (f"/api/laws/{law_id}", {"mode": "reader"}),
        (f"/api/laws/{law_id}/articles", {}),
    ):
        for paging in (
            {"page": 0}, {"page_size": 0}, {"page_size": 101},
            {"page": 1_000_001}, {"page": 10**30},
            {"page": "not-a-number"},
        ):
            params = dict(paging)
            params.update(extra)
            assert client.get(url, params=params).status_code == 422

        body = client.get(url, params={"page": 1_000_000, **extra})
        assert body.status_code == 200
        assert body.json()["articles"] == []
        assert body.json()["article_total"] == 123
        assert body.json()["article_page"] == 1_000_000
    assert client.get("/api/laws/999/articles").status_code == 404


CHAT_TOOL_MSG = {
    "role": "assistant",
    "content": None,
    "tool_calls": [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "search_laws", "arguments": '{"query": "内幕交易"}'},
    }],
}
CHAT_FINAL_MSG = {"role": "assistant", "content": "测试回答"}
CHAT_MESSAGES = [{"role": "user", "content": "内幕交易"}]


class ChatScriptedLLM:
    embedding_model = ""

    def __init__(self, script, first_call_gate=None):
        self.script = list(script)
        self.first_call_gate = first_call_gate
        self.calls = 0
        self.thread_ids = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.thread_ids.append(threading.get_ident())
        if self.calls == 1 and self.first_call_gate is not None:
            if not self.first_call_gate.wait(timeout=5):
                raise RuntimeError("测试未释放 LLM 调用闸门")
        reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_legacy_chat_keeps_sync_json_and_llm_error(client, monkeypatch):
    llm = ChatScriptedLLM([CHAT_FINAL_MSG])
    monkeypatch.setattr(main_module, "LLMClient", lambda config: llm)

    response = client.post("/api/chat", json={
        "messages": CHAT_MESSAGES, "law_id": 9,
    })

    assert response.status_code == 200
    assert response.json() == {
        "content": CHAT_FINAL_MSG["content"],
        "citations": [],
        "searches": [],
        "rounds": 1,
        "scope": {"law_id": 9},
    }

    monkeypatch.setattr(main_module, "LLMClient", lambda config:
                        ChatScriptedLLM([LLMError("网关失败")]))
    failed = client.post("/api/chat", json={"messages": CHAT_MESSAGES})
    assert failed.status_code == 502
    assert "大模型调用失败" in failed.json()["detail"]
    assert "网关失败" in failed.json()["detail"]


def test_chat_stream_emits_stages_before_llm_finishes(client, monkeypatch):
    release_llm = threading.Event()
    llm = ChatScriptedLLM(
        [CHAT_TOOL_MSG, CHAT_FINAL_MSG], first_call_gate=release_llm)
    monkeypatch.setattr(main_module, "LLMClient", lambda config: llm)
    events = []
    response_start = {}
    request_body = json.dumps({
        "messages": CHAT_MESSAGES, "law_id": 3,
    }).encode()
    request_sent = False
    disconnect = asyncio.Event()

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": request_body}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            response_start.update(message)
        elif message["type"] == "http.response.body":
            for line in message.get("body", b"").decode().splitlines():
                events.append(json.loads(line))
                if len(events) == 1:
                    # 首个 ASGI body chunk 到达后才允许 LLM 返回。
                    release_llm.set()
            if not message.get("more_body", False):
                disconnect.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/chat/stream",
        "raw_path": b"/api/chat/stream", "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    try:
        # Starlette 1.6 同步 TestClient 会缓冲完整 body；直接驱动 ASGI 验证时序。
        asyncio.run(client.app(scope, receive, send))
    finally:
        release_llm.set()

    headers = dict(response_start["headers"])
    assert response_start["status"] == 200
    assert headers[b"content-type"].startswith(b"application/x-ndjson")
    assert events == [
        {"type": "stage", "stage": "thinking", "round": 1},
        {"type": "stage", "stage": "model_done", "round": 1},
        {"type": "stage", "stage": "searching", "round": 1, "query": "内幕交易"},
        {"type": "stage", "stage": "search_done", "round": 1, "query": "内幕交易"},
        {"type": "stage", "stage": "thinking", "round": 2},
        {"type": "stage", "stage": "model_done", "round": 2},
        {"type": "stage", "stage": "citing", "round": 2},
        {"type": "result", "data": {
            "content": CHAT_FINAL_MSG["content"],
            "citations": [],
            "searches": ["内幕交易"],
            "rounds": 2,
            "scope": {"law_id": 3},
        }},
    ]
    assert llm.thread_ids[0] != threading.get_ident()
    # result 事件到达时 worker 可能正处于退出边界；轮询等待而不是断言 ASGI 必须等它。
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        alive = [thread for thread in threading.enumerate()
                 if thread.ident in llm.thread_ids and thread.is_alive()]
        if not alive:
            break
        time.sleep(0.01)
    assert not alive


def test_chat_stream_disconnect_sets_cancel_while_llm_is_blocked(client, monkeypatch):
    release_llm = threading.Event()
    first_chunk = threading.Event()
    llm = ChatScriptedLLM([CHAT_FINAL_MSG], first_call_gate=release_llm)
    monkeypatch.setattr(main_module, "LLMClient", lambda config: llm)
    captured = {}
    real_agent_chat = main_module.agent_chat

    def capture_worker(*args, **kwargs):
        captured["cancel_event"] = kwargs["cancel_event"]
        captured["worker"] = threading.current_thread()
        return real_agent_chat(*args, **kwargs)

    monkeypatch.setattr(main_module, "agent_chat", capture_worker)
    request_body = json.dumps({"messages": CHAT_MESSAGES}).encode()
    errors = []

    def run_asgi():
        async def run():
            request_sent = False
            disconnect = asyncio.Event()
            captured["loop"] = asyncio.get_running_loop()
            captured["disconnect"] = disconnect

            async def receive():
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": request_body}
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    first_chunk.set()

            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": "/api/chat/stream",
                "raw_path": b"/api/chat/stream", "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("testclient", 50000), "server": ("testserver", 80),
            }
            await client.app(scope, receive, send)

        try:
            asyncio.run(run())
        except BaseException as exc:  # 后台 ASGI 线程的异常必须带回测试线程
            errors.append(exc)

    asgi_thread = threading.Thread(target=run_asgi, daemon=True)
    asgi_thread.start()
    try:
        assert first_chunk.wait(timeout=1)
        cancel_event = captured["cancel_event"]
        captured["loop"].call_soon_threadsafe(captured["disconnect"].set)

        assert cancel_event.wait(timeout=0.2), "disconnect 后 200ms 内未设置协作取消"
        asgi_thread.join(timeout=0.2)
        assert not asgi_thread.is_alive(), "LLM 未返回时 ASGI disconnect 路径仍未结束"
        assert cancel_event.is_set()
        assert not release_llm.is_set(), "测试不应把协作取消描述为中断当前 LLM 调用"
    finally:
        if "cancel_event" in captured:
            captured["cancel_event"].set()  # 失败路径也确保 worker 能在闸门释放后退出
        release_llm.set()
        if asgi_thread.is_alive() and "loop" in captured:
            captured["loop"].call_soon_threadsafe(captured["disconnect"].set)
        asgi_thread.join(timeout=2)
        if "worker" in captured:
            captured["worker"].join(timeout=2)

    assert errors == []
    assert not asgi_thread.is_alive()
    assert captured["worker"].is_alive() is False
    assert llm.calls == 1


def test_chat_stream_turns_llm_error_into_ndjson(client, monkeypatch):
    monkeypatch.setattr(main_module, "LLMClient", lambda config:
                        ChatScriptedLLM([LLMError("网关暂时失败")]))

    with client.stream("POST", "/api/chat/stream",
                       json={"messages": CHAT_MESSAGES}) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines()]

    assert events == [
        {"type": "stage", "stage": "thinking", "round": 1},
        {"type": "error", "error": "大模型调用失败：网关暂时失败"},
    ]


def test_chat_stream_hides_unexpected_exception_details(client, monkeypatch):
    monkeypatch.setattr(main_module, "LLMClient", lambda config:
                        ChatScriptedLLM([RuntimeError("secret-config-value")]))

    with client.stream("POST", "/api/chat/stream",
                       json={"messages": CHAT_MESSAGES}) as response:
        events = [json.loads(line) for line in response.iter_lines()]

    assert events[-1] == {"type": "error", "error": "对话处理失败（RuntimeError）"}
    assert "secret-config-value" not in json.dumps(events, ensure_ascii=False)


def test_chat_stream_emits_cancelled_terminal_event(client, monkeypatch):
    monkeypatch.setattr(main_module, "LLMClient", lambda config: object())

    def cancelled_chat(*args, **kwargs):
        raise chat_module.ChatCancelled()

    monkeypatch.setattr(main_module, "agent_chat", cancelled_chat)
    with client.stream("POST", "/api/chat/stream",
                       json={"messages": CHAT_MESSAGES}) as response:
        events = [json.loads(line) for line in response.iter_lines()]

    assert events == [{"type": "cancelled"}]
