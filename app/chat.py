# app/chat.py — Agent 对话循环：LLM 通过 tool calling 自主多轮检索知识库
import json
import re

from app.search import hybrid_search, normalize_article_no

TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_laws",
        "description": "在证券法律法规知识库中检索法条。可多次调用，换不同关键词从不同角度检索。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词，如：内幕交易 行政处罚",
                },
                "top_k": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
}]

SYSTEM_PROMPT = """你是证券法律法规研究专家。回答用户关于证券业务法律法规的问题时，必须遵守：

1. 仅依据 search_laws 工具检索到的法条内容作答；检索不到就明确说明"知识库中未找到相关规定"，禁止编造。
2. 每条结论后标注来源，格式：【法规名·第X条】。
3. 需要多角度信息时，主动多次调用 search_laws 换不同关键词检索。
4. 注意法规时效状态（现行有效/已修订/已废止），引用时说明。
5. 回答用中文，条理清晰；涉及处罚、门槛、程序等关键数字要准确引用条文。"""

MAX_ROUNDS = 5


def _safe_top_k(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 8
    return max(1, min(50, n))


def _run_tool(db, llm, args: dict) -> str:
    """执行 search_laws，返回压缩后的检索结果文本。"""
    query = args.get("query") if isinstance(args.get("query"), str) else ""
    results = hybrid_search(db, llm, query, top_k=_safe_top_k(args.get("top_k", 8)))
    if not results:
        return "（未检索到相关法条）"
    lines = []
    for r in results:
        lines.append(
            f"【{r['title']}·{r['article_no'] or '全文'}】（{r['status']}）{r['text'][:500]}"
        )
    return "\n\n".join(lines)


def agent_chat(db, llm, messages: list[dict], max_rounds: int = MAX_ROUNDS) -> dict:
    """Agent 循环：LLM 决定是否检索 → 执行 → 回填 → 继续或终止。

    返回 {"content": 最终回答, "citations": 引用列表,
          "searches": [每次检索词], "rounds": 实际轮数}
    """
    # 输入防御：只保留 dict 消息、最近 40 条、单条 8k 字，避免超长上下文烧 token
    clean = [dict(m) for m in messages if isinstance(m, dict)][-40:]
    for m in clean:
        if isinstance(m.get("content"), str) and len(m["content"]) > 8000:
            m["content"] = m["content"][:8000]
    convo = [{"role": "system", "content": SYSTEM_PROMPT}] + clean
    searches: list[str] = []
    citations: list[dict] = []
    rounds = 0

    while rounds < max_rounds:
        rounds += 1
        msg = llm.chat(convo, tools=TOOLS)
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            content = msg.get("content") or ""
            citations = _extract_citations(db, content)
            return {"content": content, "citations": citations,
                    "searches": searches, "rounds": rounds}

        # 有工具调用：执行并回填（对网关/模型返回结构做防御）
        convo.append(msg)
        for idx, tc in enumerate(tool_calls):
            fn = tc.get("function") if isinstance(tc, dict) else None
            if not isinstance(fn, dict):
                continue
            name = fn.get("name") or ""
            if name == "search_laws":
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if not isinstance(args, dict):  # LLM 偶发返回数组/字符串
                    args = {}
                q = args.get("query") if isinstance(args.get("query"), str) else ""
                searches.append(q)  # 与 _run_tool 用同一规范化值
                output = _run_tool(db, llm, args)
            else:
                output = f"未知工具：{name}"
            convo.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or f"call_{rounds}_{idx}",
                "content": output,
            })

    # 轮数用尽：强制收尾（不再给工具）
    convo.append({"role": "user", "content": "（请基于已检索到的信息直接回答，不要再调用工具）"})
    final = llm.chat(convo)
    content = final.get("content") or ""
    return {"content": content, "citations": _extract_citations(db, content),
            "searches": searches, "rounds": rounds}


# 一个【】里可能放多个引用（用；分隔），例如：
# 【证监会令第227号·第七十条；证监会令第166号·第四十条】
CITATION_BLOCK_RE = re.compile(r"【([^】]+)】")
CITATION_ITEM_RE = re.compile(r"^(.+?)·(.+)$")
ARTICLE_NO_RE = re.compile(r"第[一二三四五六七八九十百千零〇\d]+条")


_ALIASES = (
    ("最高检", "最高人民检察院"),
    ("最高法", "最高人民法院"),
    ("证监会", "中国证券监督管理委员会"),
)

# 法规标题里的发文字号括号，如（证监会令第222号）、（法释〔2020〕1号）
_DOC_NO_RE = re.compile(
    r"[（(](?:证监会令|证监会公告|主席令|国务院令|财政部令|法释|银发|发改)"
    r"[^）)]*[）)]")


def _clean_title(title: str) -> str:
    """去掉书名号外层、来源前缀与发文字号，只保留法规核心名。

    例：《公司债券发行与交易管理办法》（证监会令第222号）
        -> 公司债券发行与交易管理办法
    """
    t = str(title or "").strip()
    m = re.search(r"《([^》]+)》", t)
    if m:
        t = m.group(1)
    t = re.sub(r"^【[^】]*】", "", t)
    t = _DOC_NO_RE.sub("", t)
    return t.strip().strip("《》").strip()


def _title_variants(title: str) -> list[str]:
    """标题变体：核心名、全称/简称、去掉年份版本括号。"""
    t = _clean_title(title)
    if not t:
        return []
    out = [t]
    for short, full in _ALIASES:
        if short in t:
            alt = t.replace(short, full)
            if alt not in out:
                out.append(alt)
    cleaned = re.sub(r"[（(]\d{4}年?(?:修订|修正)?[）)]", "", t).strip()
    if cleaned and cleaned not in out:
        out.append(cleaned)
    return out


def _fuzzy_keys(title: str) -> list[str]:
    """从长标题中提取可用于 LIKE 的核心片段（书名号内名称、按顿号切分）。"""
    keys: list[str] = []
    for part in re.split(r"[、，,；;]", str(title or "")):
        part = _clean_title(part)
        if not part:
            continue
        part = re.sub(r"[（(]\d{4}年?(?:修订|修正)?[）)]", "", part)
        part = part.replace("（", "(").replace("）", ")").strip("《》").strip()
        if len(part) >= 4 and part not in keys:
            keys.append(part)
    # 优先含法规类型词、较长的片段，减少误匹配到无关法规
    keys.sort(key=lambda k: (0 if re.search(
        r"规定|办法|条例|准则|通知|解释|意见|决定", k) else 1, -len(k)))
    return keys


def _match_ok(cite_title: str, law_title: str) -> bool:
    """避免把「关于修改《X》的决定」误当成 X 本身。

    引用标题本身是修改决定、或带证监会令/公告文号时允许匹配修改决定；
    否则 X 只能匹配标题不含「修改」的法规。
    """
    if re.search(r"修改|证监会令|证监会公告|主席令|国务院令|财政部令|法释",
                 cite_title):
        return True
    return "修改" not in law_title


def _find_law(db, title: str):
    """按标题找现行法规：精确变体优先，再核心片段模糊匹配。"""
    # 用原始标题判断是否明确引用“修改决定”；清洗后的核心名会丢掉“修改”语义
    cite_title = str(title or "").strip()
    sql = """SELECT id, title, source_url FROM laws
             WHERE title {expr} AND duplicate_of IS NULL
               AND superseded_by IS NULL
               AND (quality_flag IS NULL OR quality_flag != 'empty_text')
             ORDER BY length(title) LIMIT 1"""
    for v in _title_variants(title):
        for cand in (v, f"中华人民共和国{v}"):
            row = db.execute(sql.format(expr="= ?"), (cand,)).fetchone()
            if row and _match_ok(cite_title, row["title"]):
                return row
        if v.startswith("中华人民共和国"):
            row = db.execute(sql.format(expr="= ?"),
                             (v[len("中华人民共和国"):],)).fetchone()
            if row and _match_ok(cite_title, row["title"]):
                return row
    for v in _title_variants(title):
        if len(v) < 4:
            continue
        for key in (_fuzzy_keys(v) or [v]):
            if len(key) < 4:
                continue
            eq = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            row = db.execute(sql.format(expr="LIKE ? ESCAPE '\\'"),
                             (f"%{eq}%",)).fetchone()
            if row and _match_ok(cite_title, row["title"]):
                return row
    return None


def _lookup_citation(db, title: str, article_no: str) -> dict:
    """解析单条引用：matched / 仅法规命中（条号未定位）/ 完全未收录。

    无论能否命中都返回条目，保证下方 reference 列表与正文引用一一对应。
    """
    raw_title = title.strip()
    clean = _clean_title(title)
    no = normalize_article_no(article_no)
    item = {
        "title": clean, "article_no": no,
        "ref_title": raw_title, "ref_no": article_no,
        "law_id": None, "article_id": None, "source_url": None,
    }
    law = _find_law(db, raw_title)
    if law is None:
        return item
    item.update({"title": law["title"], "law_id": law["id"],
                 "source_url": law["source_url"]})
    art = db.execute(
        "SELECT id FROM articles WHERE law_id=? AND article_no=?",
        (law["id"], no)).fetchone()
    if art:
        item["article_id"] = art["id"]
        item["article_no"] = no
    return item


def _extract_citations(db, content: str) -> list[dict]:
    """提取正文全部【法规名·条号】引用，并解析为跳转定位。

    兼容：
    - 阿拉伯数字条号（第191条）与数据库中文条号（第一百九十一条）；
    - 一个【】内多个引用（用；分隔）；
    - 简称/全称/带「关于印发…通知」包装的法规标题；
    - 库内未收录的法规：保留 citation 项（law_id=None），前端标注未收录。
    """
    out: dict[tuple, dict] = {}
    for block in CITATION_BLOCK_RE.finditer(content or ""):
        for item in re.split(r"[；;]", block.group(1)):
            m = CITATION_ITEM_RE.search(item.strip())
            if not m:
                continue
            title, rest = m.group(1).strip(), m.group(2).strip()
            if rest.startswith("全文"):
                raw_nos = ["全文"]
            else:
                # 同一引用可能连写多个条号：第十四条、第二十条
                raw_nos = list(dict.fromkeys(ARTICLE_NO_RE.findall(rest)))
            for raw_no in raw_nos:
                key = (title, normalize_article_no(raw_no))
                hit = _lookup_citation(db, title, raw_no)
                prev = out.get(key)
                # 同一引用出现多次时优先保留能定位到法规的版本
                if prev is None or (prev["law_id"] is None
                                    and hit["law_id"] is not None):
                    out[key] = hit
    return list(out.values())
