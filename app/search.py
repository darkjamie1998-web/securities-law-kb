# app/search.py — 混合检索：FTS5 关键词 + 向量余弦，RRF 融合
import json

from app.llm import cosine


def _fts_search(db, query: str, top_k: int = 50) -> list[int]:
    """FTS5 关键词路：短语匹配。trigram 分词器下中文子串可命中。"""
    q = query.strip().replace('"', '""')
    if not q:
        return []
    try:
        rows = db.execute(
            'SELECT rowid FROM articles_fts WHERE articles_fts MATCH ? '
            "ORDER BY rank LIMIT ?",
            (f'"{q}"', top_k),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _vector_search(db, query_vec: list[float] | None, top_k: int = 50) -> list[int]:
    """向量路：与库内已向量化法条算余弦。query_vec 为空时跳过。"""
    if not query_vec:
        return []
    scored = []
    for row in db.execute(
        "SELECT id, embedding FROM articles WHERE embedding IS NOT NULL"
    ):
        try:
            vec = json.loads(row["embedding"])
        except json.JSONDecodeError:
            continue
        scored.append((cosine(query_vec, vec), row["id"]))
    scored.sort(reverse=True)
    return [aid for score, aid in scored[:top_k]]


def _rrf(rankings: list[list[int]], top_k: int) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: score = Σ 1/(60 + rank)。"""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, aid in enumerate(ranking):
            scores[aid] = scores.get(aid, 0.0) + 1.0 / (60 + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]


def hybrid_search(db, llm, query: str, top_k: int = 10) -> list[dict]:
    """混合检索：返回法条级结果（含法规元数据）。

    llm: LLMClient 或 None（无配置时退化为纯关键词检索）。
    """
    kw_ids = _fts_search(db, query)
    vec_ids: list[int] = []
    if llm is not None:
        try:
            qv = llm.embed([query])[0]
            vec_ids = _vector_search(db, qv)
        except Exception:
            pass  # embedding 失败退化为关键词路
    fused = _rrf([kw_ids, vec_ids], top_k)
    results = []
    for aid, score in fused:
        row = db.execute(
            """SELECT a.id, a.law_id, a.article_no, a.text,
                      l.title, l.level, l.status, l.source_url
               FROM articles a JOIN laws l ON a.law_id = l.id
               WHERE a.id = ?""",
            (aid,),
        ).fetchone()
        if row:
            results.append(dict(row) | {"score": round(score, 6)})
    return results
