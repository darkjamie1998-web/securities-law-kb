# app/search.py — 混合检索：精确条号 + jieba 分词 BM25 + trigram FTS + 向量，RRF 融合
import json
import re
import sqlite3
from pathlib import Path

import jieba

from app.llm import cosine

_CN_DIGITS = "零一二三四五六七八九"
_CN_UNITS = ["", "十", "百", "千"]


# ---------- 中文条号 ----------

def _int_to_cn(n: int) -> str:
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    out, unit = "", 0
    while n and unit < 4:
        d = n % 10
        if d:
            out = _CN_DIGITS[d] + _CN_UNITS[unit] + out
        n //= 10
        unit += 1
    return out


def _cn_to_int(s: str) -> int | None:
    s = s.replace("〇", "零")
    if s.isdigit():
        return int(s)
    section, num = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS.index(ch)
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
    return section + num or None


CITATION_RE = re.compile(
    r"《?([^《》\s，。；：？！]{2,30}?)》?\s*第([一二三四五六七八九十百千零〇\d]+)条")


def parse_citation(query: str):
    """识别「法规名+条号」，返回 (法规名, 规范条号)；无则 None。"""
    m = CITATION_RE.search(query or "")
    if not m:
        return None
    title, num = m.group(1).strip(), m.group(2)
    n = _cn_to_int(num)
    if n is None:
        return None
    return title, f"第{_int_to_cn(n)}条"


# ---------- 查询分词 ----------

def _load_stopwords() -> set[str]:
    path = Path(__file__).with_name("stopwords.txt")
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()}


_STOPWORDS = _load_stopwords()


def jieba_terms(query: str) -> list[str]:
    """查询分词：保留 ≥2 字实词，去停用词与重复词。"""
    from app.seg_index import _load_user_dict

    _load_user_dict()
    out: list[str] = []
    for w in jieba.cut_for_search(query or ""):
        w = w.strip()
        if len(w) >= 2 and w not in _STOPWORDS and w not in out:
            out.append(w)
    return out


# ---------- 精确条号 ----------

def _exact_article(db, law_title: str, article_no: str, top_k: int = 5):
    row = db.execute(
        """SELECT id, title, level, status, source_url FROM laws
           WHERE title = ? AND duplicate_of IS NULL AND superseded_by IS NULL""",
        (law_title,)).fetchone()
    if not row:
        row = db.execute(
            """SELECT id, title, level, status, source_url FROM laws
               WHERE title LIKE ? AND duplicate_of IS NULL AND superseded_by IS NULL
               ORDER BY length(title) LIMIT 1""",
            (f"%{law_title}%",)).fetchone()
    if not row:
        return []
    art = db.execute(
        "SELECT id, article_no, text FROM articles WHERE law_id=? AND article_no=?",
        (row["id"], article_no)).fetchone()
    if not art:
        return []
    return [{
        "id": art["id"], "law_id": row["id"], "article_no": art["article_no"],
        "text": art["text"], "title": row["title"], "level": row["level"] or "",
        "status": row["status"] or "", "source_url": row["source_url"] or "",
        "score": 10.0, "match_type": "exact_article",
    }][:top_k]


# ---------- 关键词路 ----------

def _split_terms(query: str) -> list[str]:
    """查询切词：按空白/标点切分，保留 ≥2 字的词。"""
    terms = [t for t in re.split(r"[\s，。、；：？！·,.:;?!]+", query) if len(t) >= 2]
    return terms or ([query] if query.strip() else [])


def _fts_search(db, query: str, top_k: int = 50) -> list[int]:
    """FTS5 trigram 路：整串短语 + 分词 OR 双路查询。"""
    q = query.strip().replace('"', '""')
    if not q:
        return []
    terms = _split_terms(query)
    match_exprs = [f'"{q}"']
    if len(terms) > 1:
        match_exprs.append(" OR ".join(
            f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms))
    seen: list[int] = []
    for expr in match_exprs:
        try:
            rows = db.execute(
                'SELECT rowid FROM articles_fts WHERE articles_fts MATCH ? '
                "ORDER BY rank LIMIT ?",
                (expr, top_k),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            if r[0] not in seen:
                seen.append(r[0])
        if len(seen) >= top_k:
            break
    return seen[:top_k]


def _seg_search(db, terms: list[str], top_k: int = 50) -> list[int]:
    """jieba 分词路：BM25 排序，2 字词也能命中。"""
    if not terms:
        return []
    expr = " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)
    try:
        rows = db.execute(
            "SELECT rowid FROM articles_seg WHERE articles_seg MATCH ? "
            "ORDER BY rank LIMIT ?", (expr, top_k)).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
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


def _like_search(db, query: str, top_k: int = 50) -> list[int]:
    """LIKE 全文兜底：单字/双字或分词索引缺失时的最后一道召回。"""
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
    """混合检索：精确条号置顶，分词 BM25 + trigram + 向量三路 RRF 融合。"""
    query = (query or "").strip()
    if not query:
        return []

    exact: list[dict] = []
    cite = parse_citation(query)
    if cite:
        exact = _exact_article(db, *cite, top_k=top_k)

    terms = jieba_terms(query)
    seg_ids = _seg_search(db, terms)
    kw_ids = _fts_search(db, query)
    if not seg_ids and not kw_ids:
        kw_ids = _like_search(db, query, top_k)

    vec_ids: list[int] = []
    if llm is not None and llm.embedding_model:
        has_embedded = db.execute(
            "SELECT 1 FROM articles WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        if has_embedded:
            try:
                qv = llm.embed([query])[0]
                vec_ids = _vector_search(db, qv)
            except Exception:
                pass  # embedding 失败退化为关键词路

    fused = _rrf([seg_ids, kw_ids, vec_ids], top_k * 3)
    rows = db.execute(
        f"""SELECT a.id, a.law_id, a.article_no, a.text,
                   l.title, l.level, l.status, l.source_url
            FROM articles a JOIN laws l ON a.law_id = l.id
            WHERE a.id IN ({",".join("?" * len(fused))})
              AND l.duplicate_of IS NULL
              AND (l.quality_flag IS NULL OR l.quality_flag != 'duplicate')""",
        [aid for aid, _ in fused],
    ).fetchall() if fused else []
    by_id = {r["id"]: dict(r) for r in rows}

    out, seen = [], set()
    for item in exact:
        if item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)
    for aid, score in fused:
        if aid in seen or aid not in by_id:
            continue
        seen.add(aid)
        out.append(by_id[aid] | {"score": round(score, 6),
                                 "match_type": "keyword"})
    return out[:top_k]
