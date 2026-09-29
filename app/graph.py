# app/graph.py — 法规级知识图谱数据构建（供 /api/graph 与前端 Canvas 渲染）
import sqlite3

# 业务板块划分：标题关键词有序匹配（先专后泛），无匹配归"其他"。
# 顺序经过冲突校验：债券<发行、收购>上市、适当性>期货 等优先级已调平；
# 注意中文子串陷阱（"公司法"含"司法"），故监管执法不用裸"司法"关键词。
# 规则透明可迭代：调整 BOARDS 后无需跑批，查询时动态计算。
BOARDS: list[tuple[str, list[str]]] = [
    ("债券固收", ["债券", "可转换", "资产支持", "国债", "回购"]),
    ("基金资管", ["基金", "资产管理", "理财", "私募", "公募", "托管人"]),
    ("投资者保护", ["投资者", "适当性"]),
    ("期货衍生品", ["期货", "衍生品", "期权", "套期保值"]),
    ("并购重组", ["收购", "并购", "重组", "要约", "合并", "分立"]),
    ("信息披露", ["披露", "定期报告", "临时公告"]),
    ("发行上市", ["发行", "上市", "承销", "保荐", "注册"]),
    ("交易结算", ["交易", "结算", "经纪", "融资融券", "转融通"]),
    ("监管执法", ["处罚", "稽查", "监察", "监管", "复议", "诉讼",
               "最高人民法院", "刑法", "检察", "公安", "赔偿", "执法", "禁入"]),
    ("税收", ["税"]),
    ("数据网络", ["数据", "网络", "密码", "电信", "电子签名", "个人信息"]),
    ("金融基础", ["银行", "货币", "信托", "保险", "票据", "洗钱", "外汇",
               "支付", "金融"]),
    ("证券综合", ["证券", "证券交易所"]),
    ("公司治理", ["公司", "股东", "董事", "监事", "治理", "章程",
               "企业", "合伙", "破产", "商事登记"]),
    ("商事通用", ["民法典", "民事", "立法", "竞争", "垄断", "拍卖", "招标",
               "海商", "产品质量", "广告", "消费者", "标准化", "预算",
               "贸易", "外商投资", "不正当", "电子商务"]),
]

BOARD_FALLBACK = "其他"


def classify_board(title: str) -> str:
    """按 BOARDS 顺序返回标题命中的第一个板块（关键词子串匹配）。"""
    for board, keywords in BOARDS:
        if any(k in title for k in keywords):
            return board
    return BOARD_FALLBACK


def build_graph(db: sqlite3.Connection) -> dict:
    """返回法规级图谱：nodes（含度数）+ edges（法规对×关系类型去重）。

    relations 表是法条级的（from_id/to_id 指向 article），图谱按法规聚合：
    同一对法规间可能存在多条法条级边，聚合后保留 (s, t, rel_type) 去重。
    """
    nodes = [
        {"id": r["id"], "title": r["title"], "level": r["level"] or "未分类",
         "status": r["status"] or "", "out_deg": r["out_deg"], "in_deg": r["in_deg"],
         "board": classify_board(r["title"])}
        for r in db.execute(
            """SELECT l.id, l.title, l.level, l.status,
                 (SELECT count(*) FROM relations r JOIN articles a ON a.id = r.from_id
                  WHERE a.law_id = l.id) AS out_deg,
                 (SELECT count(*) FROM relations r JOIN articles a ON a.id = r.to_id
                  WHERE a.law_id = l.id) AS in_deg
               FROM laws l""")
    ]
    edges = [
        {"s": r["s"], "t": r["t"], "rel_type": r["rel_type"]}
        for r in db.execute(
            """SELECT DISTINCT fa.law_id AS s, ta.law_id AS t, r.rel_type
               FROM relations r
               JOIN articles fa ON fa.id = r.from_id
               JOIN articles ta ON ta.id = r.to_id""")
    ]
    return {"nodes": nodes, "edges": edges}
