# app/search.py — 混合检索：FTS5 关键词 + 向量余弦，RRF 融合
import json
import re

from app.llm import cosine


def _split_terms(query: str) -> list[str]:
    """查询切词：按空白/标点切分，保留 ≥2 字的词（trigram 最小命中单元）。"""
    terms = [t for t in re.split(r"[\s，。、；：？！·,.:;?!]+", query) if len(t) >= 2]
    return terms or ([query] if query.strip() else [])


def _fts_search(db, query: str, top_k: int = 50) -> list[int]:
    """FTS5 关键词路：整串短语 + 分词 OR 双路查询。

    trigram 分词器下中文子串可命中；整串查不到时按词 OR 匹配。
    """
    q = query.strip().replace('"', '""')
    if not q:
        return []
    terms = _split_terms(query)
    # 双路：整串 phrase 优先（更精确），分词 >1 时加 OR 路
    match_exprs = [f'"{q}"']
    if len(terms) > 1:
        match_exprs.append(" OR ".join(f'"{t}"' for t in terms))
    seen: list[int] = []
    for expr in match_exprs:
        try:
            rows = db.execute(
                'SELECT rowid FROM articles_fts WHERE articles_fts MATCH ? '
                "ORDER BY rank LIMIT ?",
                (expr, top_k),
            ).fetchall()
        except Exception:
            continue
        for r in rows:
            if r[0] not in seen:
                seen.append(r[0])
        if len(seen) >= top_k:
            break
    return seen[:top_k]


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
