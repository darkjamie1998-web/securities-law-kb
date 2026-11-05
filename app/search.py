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
    digits = [n // 1000, n // 100 % 10, n // 10 % 10, n % 10]
    out, started, pending_zero = "", False, False
    for index, digit in enumerate(digits):
        if not digit:
            if started:
                pending_zero = True
            continue
        if started and pending_zero:
            out += "零"
        # 10–19 的开头“十”不写“一”，其余位数保留完整数字。
        if started or index != 2 or digit != 1:
            out += _CN_DIGITS[digit]
        out += _CN_UNITS[3 - index]
        started, pending_zero = True, False
    return out


def _cn_to_int(s: str) -> int | None:
    s = s.replace("〇", "零")
    if s.isdigit():
        return int(s) if len(s) <= 4 else None
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
    if n is None or not 1 <= n <= 9999:
        return None
    return title, f"第{_int_to_cn(n)}条"


def normalize_article_no(no: str) -> str:
    """规范 1–9999 的基础条号及「之一/之二」后缀，并兼容说明性括号。

    阿拉伯数字与中文数字混写、非规范中文数字或越界数字无法可靠判断原意，
    直接返回原文使其查不到，避免误跳法条。
    """
    original = str(no or "").strip()
    m = re.fullmatch(
        r"第([一二三四五六七八九十百千零〇\d]+)条"
        r"(?:之([一二三四五六七八九十百千零〇\d]+))?"
        r"(?:[（(][^（）()]+[）)])?", original)
    if not m:
        return original
    normalized = []
    for raw in m.groups():
        if raw is None:
            continue
        if re.search(r"\d", raw) and re.search(r"[一二三四五六七八九十百千零〇]", raw):
            return original
        n = _cn_to_int(raw)
        if n is None or not 1 <= n <= 9999:
            return original
        canonical = _int_to_cn(n)
        if not raw.isdigit() and raw.replace("〇", "零") != canonical:
            return original
        normalized.append(canonical)
    return f"第{normalized[0]}条" + (f"之{normalized[1]}" if len(normalized) > 1 else "")


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


# ---------- 法规过滤（与法规目录共用 topic 归一化规则） ----------

_TOPIC_TRANSLATION = str.maketrans({
    "，": ",", " ": "", "\t": "", "\n": "", "\r": "", "　": "",
})
_TOPIC_NORMALIZE_SQL = (
    "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
    "topic_tags, '，', ','), ' ', ''), char(9), ''), char(10), ''), "
    "char(13), ''), '　', '')"
)


def _normalize_topic_text(value: str) -> str:
    return value.translate(_TOPIC_TRANSLATION)


def _law_filter_sql(filters: dict | None, alias: str = "") -> tuple[list[str], list]:
    filters = filters or {}
    prefix = f"{alias}." if alias else ""
    where = [
        f"{prefix}duplicate_of IS NULL",
        f"{prefix}superseded_by IS NULL",
        f"({prefix}quality_flag IS NULL OR "
        f"{prefix}quality_flag NOT IN ('duplicate','empty_text'))",
    ]
    params = []
    for key, column in (("level", "level"), ("status", "status"),
                        ("source", "source_name"), ("law_id", "id")):
        if filters.get(key) is not None and filters.get(key) != "":
            where.append(f"{prefix}{column} = ?")
            params.append(filters[key])
    if "topic" in filters and filters["topic"] is not None:
        topic = _normalize_topic_text(str(filters["topic"]))
        if topic:
            normalized = _TOPIC_NORMALIZE_SQL.replace(
                "topic_tags", f"{prefix}topic_tags")
            where.append(f"instr(',' || {normalized} || ',', ?) > 0")
            params.append(f",{topic},")
        else:
            where.append("0")
    return where, params


# ---------- 精确条号 ----------

def _exact_article_id(db, law_title: str, article_no: str,
                      filters: dict | None = None) -> int | None:
    where, params = _law_filter_sql(filters, "l")
    cond = " AND ".join(where)
    row = db.execute(
        f"""SELECT l.id FROM laws l
            WHERE l.title = ? AND {cond}""",
        [law_title] + params).fetchone()
    if not row:
        row = db.execute(
            f"""SELECT l.id FROM laws l
                WHERE l.title LIKE ? AND {cond}
                ORDER BY length(l.title) LIMIT 1""",
            [f"%{law_title}%"] + params).fetchone()
    if not row:
        return None
    art = db.execute(
        "SELECT id FROM articles WHERE law_id=? AND article_no=?",
        (row["id"], article_no)).fetchone()
    return art["id"] if art else None


# ---------- 关键词路 ----------

def _split_terms(query: str) -> list[str]:
    """查询切词：按空白/标点切分，保留 ≥2 字的词。"""
    terms = [t for t in re.split(r"[\s，。、；：？！·,.:;?!]+", query) if len(t) >= 2]
    return terms or ([query] if query.strip() else [])


def _fts_search(db, query: str, top_k: int | None = 50) -> list[int]:
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
    limit = "" if top_k is None else " LIMIT ?"
    for expr in match_exprs:
        params = [expr] if top_k is None else [expr, top_k]
        try:
            rows = db.execute(
                'SELECT rowid FROM articles_fts WHERE articles_fts MATCH ? '
                f"ORDER BY rank{limit}", params).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            if r[0] not in seen:
                seen.append(r[0])
        if top_k is not None and len(seen) >= top_k:
            break
    return seen if top_k is None else seen[:top_k]


def _seg_search(db, terms: list[str], top_k: int | None = 50) -> list[int]:
    """jieba 分词路：BM25 排序，2 字词也能命中。"""
    if not terms:
        return []
    expr = " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)
    limit = "" if top_k is None else " LIMIT ?"
    params = [expr] if top_k is None else [expr, top_k]
    try:
        rows = db.execute(
            "SELECT rowid FROM articles_seg WHERE articles_seg MATCH ? "
            f"ORDER BY rank{limit}", params).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


def _vector_search(db, query_vec: list[float] | None,
                   top_k: int | None = 50) -> list[int]:
    """向量路：与库内已向量化法条算余弦；top_k=None 时保留全部候选。"""
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


def _like_search(db, query: str, top_k: int | None = 50) -> list[int]:
    """LIKE 全文兜底：单字/双字或分词索引缺失时的最后一道召回。"""
    terms = [t for t in re.split(r"[\s，。、；：？！·,.:;?!]+", query) if t]
    if not terms:
        return []
    cond = " OR ".join([r"a.text LIKE ? ESCAPE '\'"] * len(terms))
    score = " + ".join(
        "(length(a.text) - length(replace(a.text, ?, '')))" for _ in terms)
    limit = "" if top_k is None else " LIMIT ?"
    params = terms + [f"%{t}%" for t in terms]
    if top_k is not None:
        params.append(top_k)
    rows = db.execute(
        f"""SELECT a.id, ({score}) AS hits FROM articles a
            WHERE {cond} ORDER BY hits DESC, a.id{limit}""", params).fetchall()
    return [r[0] for r in rows]


def _rrf(rankings: list[list[int]], top_k: int | None) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: score = Σ 1/(60 + rank)。"""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, aid in enumerate(ranking):
            scores[aid] = scores.get(aid, 0.0) + 1.0 / (60 + rank)
    fused = sorted(scores.items(), key=lambda kv: -kv[1])
    return fused if top_k is None else fused[:top_k]


def _filter_candidate_ids(db, article_ids: list[int],
                          filters: dict | None) -> set[int]:
    """第一阶段只取过滤后的候选 ID，不为非当前页结果读取正文。"""
    where, filter_params = _law_filter_sql(filters, "l")
    cond = " AND ".join(where)
    valid_ids: set[int] = set()
    for start in range(0, len(article_ids), 500):
        chunk = article_ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = db.execute(
            f"""SELECT a.id FROM articles a JOIN laws l ON a.law_id = l.id
                WHERE a.id IN ({placeholders}) AND {cond}""",
            chunk + filter_params,
        ).fetchall()
        valid_ids.update(r["id"] for r in rows)
    return valid_ids


def _candidate_rows(db, article_ids: list[int], filters: dict | None) -> dict[int, dict]:
    """第二阶段分批读取当前页正文，避免超过 SQLite 变量上限。"""
    where, filter_params = _law_filter_sql(filters, "l")
    cond = " AND ".join(where)
    by_id: dict[int, dict] = {}
    for start in range(0, len(article_ids), 500):
        chunk = article_ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = db.execute(
            f"""SELECT a.id, a.law_id, a.article_no, a.text,
                       l.title, l.level, l.status, l.source_url
                FROM articles a JOIN laws l ON a.law_id = l.id
                WHERE a.id IN ({placeholders}) AND {cond}""",
            chunk + filter_params,
        ).fetchall()
        by_id.update({r["id"]: dict(r) for r in rows})
    return by_id


def _highlight_terms(query: str) -> list[str]:
    terms = []
    for term in _split_terms(query) + jieba_terms(query):
        term = term.strip()
        if len(term) >= 2 and term not in _STOPWORDS and term not in terms:
            terms.append(term)
    return sorted(terms, key=len, reverse=True)


def _make_snippet(text: str, terms: list[str], limit: int = 260) -> str:
    """以正文里最早的真实命中为中心截取纯文本片段。"""
    text = text or ""
    if len(text) <= limit:
        return text
    folded = text.casefold()
    hit_pos, hit_len = None, 0
    for term in terms:  # 已按长度降序，同位置优先保留长词
        pos = folded.find(term.casefold())
        if pos >= 0 and (hit_pos is None or pos < hit_pos):
            hit_pos, hit_len = pos, len(term)
    if hit_pos is None:
        return text[:limit]
    center = hit_pos + hit_len // 2
    start = max(0, min(len(text) - limit, center - limit // 2))
    return text[start:start + limit]


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def hybrid_search_page(db, llm, query: str, page: int = 1, page_size: int = 10,
                       filters: dict | None = None) -> dict:
    """全量融合候选后过滤、分页；total 仅表示本次实际召回的候选总数。"""
    page = _positive_int(page, 1)
    page_size = min(100, _positive_int(page_size, 10))
    query = (query or "").strip()
    if not query:
        return {"results": [], "total": 0, "page": page, "page_size": page_size,
                "has_more": False, "mode": "keyword"}

    cite = parse_citation(query)
    exact_id = _exact_article_id(db, *cite, filters=filters) if cite else None

    terms = jieba_terms(query)
    seg_ids = _seg_search(db, terms, top_k=None)
    kw_ids = _fts_search(db, query, top_k=None)
    if not seg_ids and not kw_ids:
        kw_ids = _like_search(db, query, top_k=None)

    vec_ids: list[int] = []
    if llm is not None and llm.embedding_model:
        has_embedded = db.execute(
            "SELECT 1 FROM articles WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        if has_embedded:
            try:
                qv = llm.embed([query])[0]
                vec_ids = _vector_search(db, qv, top_k=None)
            except Exception:
                pass  # embedding 失败退化为关键词路

    fused = _rrf([seg_ids, kw_ids, vec_ids], top_k=None)
    scores = dict(fused)
    candidate_ids, seen = [], set()
    if exact_id is not None:
        candidate_ids.append(exact_id)
        seen.add(exact_id)
    for aid, _ in fused:
        if aid not in seen:
            seen.add(aid)
            candidate_ids.append(aid)

    valid_ids = _filter_candidate_ids(db, candidate_ids, filters)
    ordered_ids = [aid for aid in candidate_ids if aid in valid_ids]
    total = len(ordered_ids)
    offset = (page - 1) * page_size
    page_ids = ordered_ids[offset:offset + page_size]
    by_id = _candidate_rows(db, page_ids, filters)
    highlights = _highlight_terms(query)
    reason_sets = {
        "segment": set(seg_ids), "keyword": set(kw_ids), "vector": set(vec_ids),
    }

    results = []
    for aid in page_ids:
        if aid not in by_id:
            continue
        is_exact = aid == exact_id
        item = by_id[aid] | {
            "score": 10.0 if is_exact else round(scores[aid], 6),
            "match_type": "exact_article" if is_exact else "keyword",
        }
        if is_exact:
            for key in ("level", "status", "source_url"):
                item[key] = item[key] or ""
        reasons = ["exact_article"] if is_exact else []
        reasons.extend(reason for reason in ("segment", "keyword", "vector")
                       if aid in reason_sets[reason])
        item["match_reasons"] = reasons
        item["highlight_terms"] = highlights
        item["snippet"] = _make_snippet(item["text"], highlights)
        results.append(item)

    return {
        "results": results, "total": total, "page": page, "page_size": page_size,
        "has_more": offset + len(results) < total,
        "mode": "hybrid" if vec_ids else "keyword",
    }


def hybrid_search(db, llm, query: str, top_k: int = 10,
                  filters: dict | None = None) -> list[dict]:
    """兼容对话与旧调用：返回分页检索内核的第一页。"""
    return hybrid_search_page(
        db, llm, query, page=1, page_size=top_k, filters=filters)["results"]
