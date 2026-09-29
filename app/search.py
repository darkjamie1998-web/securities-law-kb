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


def _like_search(db, query: str, top_k: int = 50) -> list[int]:
    """LIKE 全文兜底：trigram FTS 只支持 ≥3 字查询，"税"这类单字/双字词
    以及 FTS 未命中的长尾查询（生僻组合）降级为 LIKE 扫描。

    评分 = 各词出现次数之和（SQLite length()/replace() 均按字符计），
    命中多的条文排前。2 万行本地扫描实测 <200ms，可接受。
    """
    terms = [t for t in re.split(r"[\s，。、；：？！·,.:;?!]+", query) if t]
    if not terms:
        return []
    cond = " OR ".join([r"a.text LIKE ? ESCAPE '\'"] * len(terms))
    score = " + ".join(
        "(length(a.text) - length(replace(a.text, ?, '')))" for _ in terms)
    rows = db.execute(
        f"""SELECT a.id, ({score}) AS hits FROM articles a
            WHERE {cond} ORDER BY hits DESC, a.id LIMIT ?""",
        terms + [f"%{t}%" for t in terms] + [top_k],
    ).fetchall()
    return [r[0] for r in rows]


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
    if not kw_ids:
        # 兜底：短词（<3 字，trigram 无法命中）或 FTS 未命中的查询走 LIKE 扫描
        kw_ids = _like_search(db, query, top_k)
    vec_ids: list[int] = []
    if llm is not None and llm.embedding_model:
        # 仅当库内已有向量化法条时才发起查询向量计算
        # （embedding_model 为空或库内无向量时，发起的是注定失败的网关请求）
        has_embedded = db.execute(
            "SELECT 1 FROM articles WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        if has_embedded:
            try:
                qv = llm.embed([query])[0]
                vec_ids = _vector_search(db, qv)
            except Exception:
                pass  # embedding 失败退化为关键词路
    fused = _rrf([kw_ids, vec_ids], top_k)
    if not fused:
        return []
    # 一次 IN 查询取回全部元数据（避免逐条 N+1 往返）
    rows = db.execute(
        f"""SELECT a.id, a.law_id, a.article_no, a.text,
                   l.title, l.level, l.status, l.source_url
            FROM articles a JOIN laws l ON a.law_id = l.id
            WHERE a.id IN ({",".join("?" * len(fused))})""",
        [aid for aid, _ in fused],
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[aid] | {"score": round(score, 6)}
            for aid, score in fused if aid in by_id]
