"""从本机知识库提取统一、可重复生成的精选离线演示 fixture。

在仓库根目录运行：.venv/Scripts/python.exe -m tools.roadshow.extract_fixture
Stats 必须来自正在运行的本机服务；服务不可用时报 NEEDS_CONTEXT，不伪造统计。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

from app.graph import classify_board


ROOT = Path(__file__).resolve().parents[2]
DB_URI = "file:data/knowledge.db?mode=ro"
STATS_URL = "http://127.0.0.1:8000/api/stats"
OUTPUT = Path(__file__).with_name("fixture.json")
LAW_IDS = (20, 215, 216, 293, 296, 302, 406, 820)
FULL_ARTICLE_LAW_IDS = (216, 293, 302, 406, 820)
DISCLAIMER = "精选离线演示，具体事项需核对官方最新原文"
LAW_METADATA_COLUMNS = (
    "id", "title", "doc_number", "issuer", "level", "topic_tags",
    "issue_date", "published_at", "effective_date", "status", "source_name",
    "source_url", "content_hash", "created_at", "updated_at", "quality_flag",
    "duplicate_of", "superseded_by", "version_label",
)


def fetch_stats(url: str = STATS_URL) -> dict:
    """读取全库统计快照；网络或响应错误明确向上报告。"""

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            stats = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError, TimeoutError, UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise RuntimeError(
            f"NEEDS_CONTEXT: 无法读取或解析 {url} 的全库统计，"
            "请按 AGENTS.md 启动本机服务"
        ) from error
    if not isinstance(stats, dict):
        raise RuntimeError(f"NEEDS_CONTEXT: {url} 未返回 JSON 对象")
    required_fields = ("laws", "articles", "total_laws")
    invalid_fields = [
        key for key in required_fields if type(stats.get(key)) is not int
    ]
    if invalid_fields:
        raise RuntimeError(
            f"NEEDS_CONTEXT: {url} 缺少有效统计字段（需为整数）："
            + ", ".join(invalid_fields)
        )
    stats["scope"] = "full_library_snapshot"
    return stats


def extract_laws(conn: sqlite3.Connection) -> dict[str, dict]:
    placeholders = ",".join("?" for _ in LAW_IDS)
    columns = ", ".join(LAW_METADATA_COLUMNS)
    rows = conn.execute(
        f"SELECT {columns} FROM laws WHERE id IN ({placeholders}) ORDER BY id",
        LAW_IDS,
    ).fetchall()
    if {row["id"] for row in rows} != set(LAW_IDS):
        missing = sorted(set(LAW_IDS) - {row["id"] for row in rows})
        raise RuntimeError(f"精选法规缺失：{missing}")
    # full_text 与 articles 重复且可能包含未选取条文，因此不放入元数据。
    return {str(row["id"]): dict(row) for row in rows}


def extract_articles(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    articles: dict[str, list[dict]] = {}
    for law_id in LAW_IDS:
        if law_id == 20:
            rows = conn.execute(
                """SELECT id, law_id, article_no, text FROM articles
                   WHERE law_id = ? ORDER BY id LIMIT 60""",
                (law_id,),
            )
        elif law_id in (215, 296):
            rows = conn.execute(
                """SELECT id, law_id, article_no, text FROM articles
                   WHERE law_id = ? AND instr(text, '减持') > 0 ORDER BY id""",
                (law_id,),
            )
        elif law_id in FULL_ARTICLE_LAW_IDS:
            # 820 的 unsplit 全文也在此原样保留，不拆条、不截断。
            rows = conn.execute(
                """SELECT id, law_id, article_no, text FROM articles
                   WHERE law_id = ? ORDER BY id""",
                (law_id,),
            )
        else:  # LAW_IDS 固定，防御后续修改漏配提取规则。
            raise RuntimeError(f"未配置法规 {law_id} 的条文提取规则")
        articles[str(law_id)] = [dict(row) for row in rows]
    return articles


def extract_relations(conn: sqlite3.Connection) -> list[dict]:
    """通过 articles 双 JOIN 保留精选法规之间的真实法条关系。"""

    placeholders = ",".join("?" for _ in LAW_IDS)
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT r.id, r.from_id, r.to_id, r.rel_type, r.note, r.created_at,
                       fa.law_id AS from_law_id, ta.law_id AS to_law_id,
                       ft.title AS from_law_title, fa.article_no AS from_article_no,
                       lt.title AS to_law_title, ta.article_no AS to_article_no
                FROM relations r
                JOIN articles fa ON fa.id = r.from_id
                JOIN articles ta ON ta.id = r.to_id
                JOIN laws ft ON ft.id = fa.law_id
                JOIN laws lt ON lt.id = ta.law_id
                WHERE fa.law_id IN ({placeholders})
                  AND ta.law_id IN ({placeholders})
                ORDER BY r.id""",
            LAW_IDS + LAW_IDS,
        )
    ]


def build_graph(laws: dict[str, dict], relations: list[dict]) -> dict:
    edge_keys = sorted({
        (r["from_law_id"], r["to_law_id"], r["rel_type"]) for r in relations
    })
    nodes = []
    for law_id in LAW_IDS:
        law = laws[str(law_id)]
        nodes.append({
            "id": law_id,
            "title": law["title"],
            "level": law["level"] or "未分类",
            "status": law["status"] or "",
            "board": classify_board(law["title"]),
            "out_deg": sum(r["from_law_id"] == law_id for r in relations),
            "in_deg": sum(r["to_law_id"] == law_id for r in relations),
        })
    return {
        "nodes": nodes,
        "edges": [
            {"s": source, "t": target, "rel_type": rel_type}
            for source, target, rel_type in edge_keys
        ],
    }


def make_citation(
    laws: dict[str, dict], article_by_id: dict[str, dict], article_id: int,
    ref_title: str,
) -> dict:
    article = article_by_id.get(str(article_id))
    if article is None:
        raise RuntimeError(f"固定引用条文缺失：article_id={article_id}")
    law = laws[str(article["law_id"])]
    return {
        "law_id": law["id"],
        "article_id": article["id"],
        "title": law["title"],
        "article_no": article["article_no"],
        "source_url": law["source_url"],
        "ref_title": ref_title,
        "ref_no": article["article_no"],
    }


def build_chats(laws: dict[str, dict], article_by_id: dict[str, dict]) -> dict:
    reduction_title = "关于修改部分证券期货规章的决定"
    disclosure_title = "上市公司信息披露暂缓与豁免管理规定"
    return {
        "reduction": {
            "question": "大股东减持计划需要披露哪些内容？",
            "content": (
                "大股东计划通过证券交易所集中竞价交易或者大宗交易方式减持股份的，"
                "应当在首次卖出前十五个交易日向证券交易所报告并披露减持计划。"
                "减持计划包括拟减持股份的数量、来源，减持的时间区间、价格区间、"
                "方式和原因，相关情形说明，以及证券交易所规定的其他内容；实施完毕"
                "或减持时间区间届满后的报告、公告要求，请直接核对条文原文"
                f"【{reduction_title}·第九条】。\n\n{DISCLAIMER}。"
            ),
            "searches": ["减持计划"],
            "citations": [
                make_citation(laws, article_by_id, 9349, reduction_title),
            ],
        },
        "disclosure": {
            "question": "信息披露暂缓、豁免有哪些要求？",
            "content": (
                "上市公司和其他信息披露义务人应当审慎确定暂缓、豁免事项，履行内部"
                "审核程序后实施；暂缓、豁免事项范围原则上应与公司股票首次上市时保持"
                "一致，上市后拟增加事项应有确实充分的证据"
                f"【{disclosure_title}·第四条】。\n\n"
                "暂缓、豁免披露商业秘密后，出现原因已消除、有关信息难以保密、信息"
                "已经泄露或者市场出现传闻等条文列明情形之一时，应当及时披露"
                f"【{disclosure_title}·第八条】。\n\n{DISCLAIMER}。"
            ),
            "searches": ["信息披露 暂缓 豁免"],
            "citations": [
                make_citation(laws, article_by_id, 13717, disclosure_title),
                make_citation(laws, article_by_id, 13721, disclosure_title),
            ],
        },
    }


def build_wikis() -> dict[str, dict]:
    """仅归纳已选入的真实条文，不生成实时模型解读或额外法律结论。"""

    return {
        "293": {
            "law_id": 293,
            "markdown": f"""# 上市公司信息披露暂缓与豁免管理规定：条文导读

> 本页是依据精选真实条文整理的离线导读，不是实时模型回答，也不替代官方原文。

## 适用与基本原则

- 第二条说明本规定适用于暂缓、豁免披露临时报告，以及在定期报告、临时报告中豁免披露规定或要求披露的内容。
- 第三条要求真实、准确、完整、及时、公平地披露信息，不得滥用暂缓或者豁免披露规避义务、误导投资者。

## 内部审核与事项范围

- 第四条要求审慎确定暂缓、豁免事项，并履行内部审核程序后实施。
- 第四条同时规定，事项范围原则上与公司股票首次在证券交易所上市时保持一致；上市后拟增加事项，应有确实充分的证据。
- 第十一条要求上市公司制定相应管理制度，明确内部审核程序，并经董事会审议通过。

## 国家秘密与商业秘密条文

- 第五条规定，有确实充分的证据证明拟披露信息涉及国家秘密等事项的，依法豁免披露。
- 第七条列明涉及商业秘密且尚未公开或者泄露时，可以暂缓或者豁免披露的具体情形。
- 第九条说明定期报告和临时报告可采用代称、汇总概括或者隐去关键信息等方式处理；临时报告处理后仍存在泄密风险时的安排，以原文为准。

## 后续披露与登记

- 第八条列明商业秘密暂缓、豁免披露后应及时披露的情形：原因已消除、有关信息难以保密、信息已经泄露或者市场出现传闻。
- 第十条要求暂缓披露原因消除后及时披露，并说明条文列示事项。
- 第十二条规定董事会秘书及时登记入档、董事长签字确认，以及登记材料保存期限要求。

{DISCLAIMER}。
""",
            "model_used": "预生成条文摘要",
            "generated_at": None,
        }
    }


def build_fixture(conn: sqlite3.Connection, stats: dict) -> dict:
    laws = extract_laws(conn)
    articles = extract_articles(conn)
    article_by_id = {
        str(article["id"]): dict(article)
        for items in articles.values()
        for article in items
    }
    relations = extract_relations(conn)
    return {
        "schema_version": 1,
        "data_scope": "curated_offline_demo",
        "notice": "交互数据为精选离线演示；stats 单独标记为本机全库统计快照。",
        "laws": laws,
        "articles": articles,
        "article_by_id": article_by_id,
        "relations": relations,
        "graph": build_graph(laws, relations),
        "stats": stats,
        "chats": build_chats(laws, article_by_id),
        "wikis": build_wikis(),
    }


def main() -> int:
    if Path.cwd().resolve() != ROOT:
        print(f"FATAL 请在仓库根目录运行，以便只读打开 {DB_URI}", file=sys.stderr)
        return 2
    try:
        stats = fetch_stats()
        conn = sqlite3.connect(DB_URI, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            data = build_fixture(conn, stats)
        finally:
            conn.close()
    except (RuntimeError, sqlite3.Error) as error:
        print(f"FATAL {error}", file=sys.stderr)
        return 2

    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    OUTPUT.write_text(payload, encoding="utf-8", newline="\n")
    article_count = sum(len(items) for items in data["articles"].values())
    print(
        f"已生成 {OUTPUT}：精选 {len(data['laws'])} 部法规、{article_count} 条正文、"
        f"{len(data['relations'])} 条关系；全库统计 {data['stats']['laws']} 部法规。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
