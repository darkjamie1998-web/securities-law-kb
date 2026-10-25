"""检索质量回归：精确条号、中文问句、短词兜底。"""
import pytest

from app.db import get_db, init_db
from app.seg_index import ensure_schema, rebuild_all
from app.search import parse_citation, hybrid_search, normalize_article_no


@pytest.fixture
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    ensure_schema(conn)
    laws = [
        ("中华人民共和国证券法", [
            ("第五十条", "禁止证券交易内幕信息的知情人和非法获取内幕信息的人利用内幕信息从事证券交易活动。"),
            ("第一百九十一条", "证券交易内幕信息的知情人或者非法获取内幕信息的人违反本法第五十三条的规定从事内幕交易的，责令依法处理非法持有的证券，没收违法所得，并处以罚款。"),
        ]),
        ("中华人民共和国刑法", [
            ("第一百八十条", "证券、期货交易内幕信息的知情人员或者非法获取证券、期货交易内幕信息的人员，在涉及证券的发行，证券、期货交易或者其他对证券、期货交易价格有重大影响的信息尚未公开前，买入或者卖出该证券，情节严重的，处五年以下有期徒刑或者拘役。"),
        ]),
        ("上市公司股东减持股份管理暂行办法", [
            ("第六条", "大股东减持股份应当规范、理性、有序，充分关注上市公司及中小股东的利益。"),
            ("第七条", "上市公司应当及时了解股东减持本公司股份的情况，主动做好规则提示。"),
        ]),
        ("关于短线交易监管的若干规定", [
            ("第七条", "短线交易认定涉及的证券，按照股票、存托凭证、可交换公司债券、可转换公司债券等各品种的持有数量分别予以计算。"),
        ]),
        ("中华人民共和国税收征收管理法", [
            ("第一条", "为了加强税收征收管理，规范税收征收和缴纳行为，保障国家税收收入。"),
        ]),
    ]
    for i, (title, arts) in enumerate(laws, 1):
        cur = conn.execute(
            "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
            (title, "法律", f"h{i}"))
        lid = cur.lastrowid
        for no, text in arts:
            conn.execute(
                "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
                (lid, no, text))
    conn.commit()
    rebuild_all(conn)
    yield conn
    conn.close()


def test_parse_citation_variants():
    assert parse_citation("证券法第五十条") == ("证券法", "第五十条")
    assert parse_citation("《中华人民共和国证券法》第一百九十一条") == (
        "中华人民共和国证券法", "第一百九十一条")
    assert parse_citation("证券法第50条") == ("证券法", "第五十条")
    assert parse_citation("内幕交易怎么处罚") is None


def test_normalize_article_no():
    assert normalize_article_no("第191条") == "第一百九十一条"
    assert normalize_article_no("第一百九十一条") == "第一百九十一条"
    assert normalize_article_no("第50条（经修正案修改）") == "第五十条"
    # 混合写法的笔误不猜测，避免误跳成第十一条
    assert normalize_article_no("第19十一条") == "第19十一条"


def test_exact_article_ranks_first(db):
    results = hybrid_search(db, None, "证券法第五十条", top_k=5)
    assert results
    assert results[0]["title"] == "中华人民共和国证券法"
    assert results[0]["article_no"] == "第五十条"
    assert results[0]["match_type"] == "exact_article"


def test_natural_language_hits_insider_trading(db):
    results = hybrid_search(db, None, "内幕交易怎么处罚", top_k=5)
    titles = {r["title"] for r in results}
    assert "中华人民共和国证券法" in titles
    assert "中华人民共和国刑法" in titles


def test_natural_language_hits_reduction(db):
    results = hybrid_search(db, None, "上市公司股东减持新规有什么要求", top_k=5)
    assert any("减持" in r["title"] for r in results)


def test_short_word_fallback(db):
    results = hybrid_search(db, None, "税", top_k=5)
    assert any("税收" in r["title"] for r in results)


def test_two_char_keyword_hits_seg(db):
    results = hybrid_search(db, None, "减持", top_k=5)
    assert any("减持" in r["title"] for r in results)


def test_empty_and_special_queries_do_not_crash(db):
    assert hybrid_search(db, None, "", 5) == []
    assert isinstance(hybrid_search(db, None, '"()*', 5), list)
    assert isinstance(hybrid_search(db, None, "x" * 500, 5), list)
