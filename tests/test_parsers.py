# tests/test_parsers.py — 法条切分测试（纯本地，不联网）
from crawler.parsers import split_articles, content_hash, extract_text_from_html


def test_split_articles_basic():
    full_text = (
        "第一章 总则\n"
        "第一条 为了规范证券发行和交易行为，保护投资者的合法权益，制定本法。\n"
        "第二条 在中华人民共和国境内，股票、公司债券的发行和交易，适用本法。\n"
    )
    arts = split_articles(full_text)
    assert len(arts) == 2
    assert arts[0][0] == "第一条"
    assert "证券发行" in arts[0][1]
    assert arts[1][0] == "第二条"
    assert "公司债券" in arts[1][1]


def test_split_articles_complex_numbers():
    full_text = (
        "第九十九条 …。\n"
        "第一百条 …。\n"
        "第一百零一条 …。\n"
        "第一百一十条之一 特别规定。\n"
    )
    arts = split_articles(full_text)
    assert [a[0] for a in arts] == [
        "第九十九条", "第一百条", "第一百零一条", "第一百一十条之一",
    ]


def test_split_articles_inline_newlines_kept():
    full_text = "第一条 内容甲。\n前款规定，适用下列情形。"
    arts = split_articles(full_text)
    assert len(arts) == 1
    assert "适用下列情形" in arts[0][1]


def test_split_articles_skips_falsy():
    assert split_articles("本决定自公布之日起施行。") == []
    assert split_articles("") == []


def test_split_articles_no_number_returns_none():
    """无'条'结构的文本：由调用方决定是否整篇入库，split 本身返回空。"""
    assert split_articles("问：某上市公司拟… 答：根据《证券法》…") == []


def test_content_hash_stable():
    assert content_hash("中文内容") == content_hash("中文内容")
    assert content_hash("中文内容") != content_hash("中文内容2")


def test_extract_text_from_html():
    html = "<html><body><div>  第一条  内容  </div><script>var x=1;</script></body></html>"
    text = extract_text_from_html(html)
    assert "第一条" in text
    assert "var x" not in text
