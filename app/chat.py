# app/chat.py — Agent 对话循环：LLM 通过 tool calling 自主多轮检索知识库
import json
import re

from app.search import hybrid_search

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
    convo = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
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

        # 有工具调用：执行并回填
        convo.append(msg)
        for tc in tool_calls:
            fn = tc["function"]
            if fn["name"] == "search_laws":
                try:
                    args = json.loads(fn["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):  # LLM 偶发返回数组/字符串
                    args = {}
                q = args.get("query") if isinstance(args.get("query"), str) else ""
                searches.append(q)  # 与 _run_tool 用同一规范化值
                output = _run_tool(db, llm, args)
            else:
                output = f"未知工具：{fn['name']}"
            convo.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": output,
            })

    # 轮数用尽：强制收尾（不再给工具）
    convo.append({"role": "user", "content": "（请基于已检索到的信息直接回答，不要再调用工具）"})
    final = llm.chat(convo)
    content = final.get("content") or ""
    return {"content": content, "citations": _extract_citations(db, content),
            "searches": searches, "rounds": rounds}


CITATION_RE = re.compile(r"【([^】]+?)·(第[^条】]+条)】")


def _extract_citations(db, content: str) -> list[dict]:
    """从回答文本提取【法规名·条号】引用，并解析为可跳转的法条定位。"""
    out, seen = [], set()
    for m in CITATION_RE.finditer(content):
        title, article_no = m.group(1).strip(), m.group(2).strip()
        key = (title, article_no)
        if key in seen:
            continue
        seen.add(key)
        row = db.execute(
            """SELECT a.id AS article_id, a.article_no, l.id AS law_id, l.title,
                      l.source_url
               FROM articles a JOIN laws l ON a.law_id = l.id
               WHERE l.title LIKE ? AND a.article_no = ? LIMIT 1""",
            (f"%{title}%", article_no),
        ).fetchone()
        if row:
            out.append({
                "article_id": row["article_id"], "law_id": row["law_id"],
                "title": row["title"], "article_no": row["article_no"],
                "source_url": row["source_url"],
            })
    return out
