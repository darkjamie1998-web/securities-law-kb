# tests/test_graph.py — 法规级图谱构建测试
import pytest

from app.db import get_db, init_db
from app.graph import build_graph, classify_board


@pytest.fixture()
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    return conn


def _seed(db):
    """3 部法规；法条级关系：A1→B1(引用)、A1→B2(引用)、B1→C1(上位法)。"""
    ids = []
    for i, title in enumerate(["法A", "法B", "法C"]):
        cur = db.execute(
            "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
            (title, "法律" if i == 0 else "部门规章", f"h{i}"))
        ids.append(cur.lastrowid)
    arts = []
    for law_id in ids:
        for no in ("第一条", "第二条"):
            cur = db.execute(
                "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
                (law_id, no, "内容"))
            arts.append(cur.lastrowid)
    db.executemany(
        "INSERT INTO relations(from_id, to_id, rel_type, note) VALUES(?,?,?,?)",
        [(arts[0], arts[2], "引用", "n"), (arts[1], arts[3], "引用", "n"),
         (arts[2], arts[4], "上位法", "n")])
    db.commit()
    return ids, arts


def test_build_graph_nodes_with_degree(db):
    ids, _ = _seed(db)
    g = build_graph(db)
    by_id = {n["id"]: n for n in g["nodes"]}
    assert len(g["nodes"]) == 3
    assert by_id[ids[0]]["out_deg"] == 2 and by_id[ids[0]]["in_deg"] == 0
    assert by_id[ids[1]]["out_deg"] == 1 and by_id[ids[1]]["in_deg"] == 2
    assert by_id[ids[2]]["out_deg"] == 0 and by_id[ids[2]]["in_deg"] == 1
    assert by_id[ids[0]]["level"] == "法律"


def test_build_graph_edges_aggregated_to_law_level(db):
    ids, _ = _seed(db)
    g = build_graph(db)
    # 法条级 3 条边 → 法规级去重：A→B 两条(引用)合并为 1，B→C 1 条(上位法)
    pairs = sorted((e["s"], e["t"], e["rel_type"]) for e in g["edges"])
    assert pairs == [(ids[0], ids[1], "引用"), (ids[1], ids[2], "上位法")]


def test_build_graph_empty(db):
    g = build_graph(db)
    assert g == {"nodes": [], "edges": []}


# ---- 业务板块分类（有序关键词匹配） ----

@pytest.mark.parametrize("title,board", [
    ("中华人民共和国证券法", "证券综合"),          # 泛证券文件归证券综合
    ("首次公开发行股票注册管理办法", "发行上市"),
    ("证券交易规则", "交易结算"),
    ("上市公司信息披露管理办法", "信息披露"),       # "披露"优先于"上市"（业务主体词）
    ("上市公司收购管理办法", "并购重组"),           # "收购"优先于"上市"
    ("公司债券发行与交易管理办法", "债券固收"),     # "债券"优先于"发行/交易"
    ("中华人民共和国证券投资基金法", "基金资管"),
    ("期货交易管理条例", "期货衍生品"),             # "期货"优先于"交易"
    ("证券期货投资者适当性管理办法", "投资者保护"), # "适当性"优先于"期货"
    ("证券市场禁入规定", "监管执法"),
    ("中华人民共和国增值税法", "税收"),
    ("中华人民共和国税收征收管理法", "税收"),
    ("中华人民共和国商业银行法", "金融基础"),
    ("中华人民共和国数据安全法", "数据网络"),
    ("中华人民共和国电子商务法", "商事通用"),       # 不被"电子签名"的"电子"误伤
    ("中华人民共和国公司法", "公司治理"),           # 不被"司法"子串误伤
    ("中华人民共和国民法典", "商事通用"),
    ("中华人民共和国反不正当竞争法", "商事通用"),
    ("某培训中心招生简章", "其他"),
])
def test_classify_board(title, board):
    assert classify_board(title) == board


def test_build_graph_nodes_have_board(db):
    _seed(db)
    g = build_graph(db)
    assert all("board" in n for n in g["nodes"])
    # 法A/法B/法C 均无板块关键词 → 归"其他"
    assert {n["board"] for n in g["nodes"]} == {"其他"}

