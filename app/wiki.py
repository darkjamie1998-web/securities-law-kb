# app/wiki.py — 法规 wiki 解读：按需生成 + 缓存复用
import re
import sqlite3
from datetime import datetime

from app.llm import LLMClient

WIKI_PROMPT = """你是证券法律法规专家。为以下法规撰写一篇 wiki 式解读文章，用 Markdown 输出。

法规标题：{title}
发文机关：{issuer}
效力级别：{level}　时效状态：{status}
发布/施行日期：{issue_date} / {effective_date}
{relation_ctx}
法规内容：
{digest}

文章结构（一级标题用 ##）：
## 立法目的与背景
## 核心制度框架
## 重点条文解读（挑 5-8 条最重要的）
## 与其他法规的衔接
## 实务要点

要求：中文；基于法规原文，不编造；关键结论标注条号；控制在 1500 字以内。"""


def _digest(db: sqlite3.Connection, law_id: int, max_chars: int = 6000) -> str:
    text = db.execute(
        "SELECT full_text FROM laws WHERE id=?", (law_id,)
    ).fetchone()["full_text"] or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars * 2 // 3] + "\n……（中略）……\n" + text[-max_chars // 3:]


def _relation_context(db: sqlite3.Connection, law_id: int) -> str:
    rows = db.execute(
        """SELECT r.rel_type, r.note, lt.title AS to_title
           FROM relations r
           JOIN articles fa ON fa.id = r.from_id
           JOIN articles la ON la.id = r.to_id
           JOIN laws lt ON lt.id = la.law_id
           WHERE fa.law_id = ? LIMIT 10""",
        (law_id,),
    ).fetchall()
    if not rows:
        return ""
    lines = [f"- 与《{r['to_title']}》关系：{r['rel_type']}。{r['note']}" for r in rows]
    return "已知的关联法规：\n" + "\n".join(lines) + "\n"


def get_or_generate(db: sqlite3.Connection, llm: LLMClient, law_id: int,
                    force: bool = False) -> dict:
    """有缓存直接返回；否则生成并缓存。force=True 强制重新生成。"""
    law = db.execute("SELECT * FROM laws WHERE id=?", (law_id,)).fetchone()
    if not law:
        return {"error": "法规不存在"}

    if not force:
        cached = db.execute(
            "SELECT markdown, model_used, generated_at FROM wiki_pages WHERE law_id=?",
            (law_id,),
        ).fetchone()
        if cached:
            return {"cached": True, **dict(cached)}

    msg = llm.chat([{"role": "user", "content": WIKI_PROMPT.format(
        title=law["title"], issuer=law["issuer"] or "未知",
        level=law["level"], status=law["status"],
        issue_date=law["issue_date"] or "未知",
        effective_date=law["effective_date"] or "未知",
        relation_ctx=_relation_context(db, law_id),
        digest=_digest(db, law_id),
    )}])
    markdown = (msg.get("content") or "").strip()
    if not markdown:
        return {"error": "生成失败：模型返回为空"}

    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        """INSERT INTO wiki_pages(law_id, markdown, model_used, generated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(law_id) DO UPDATE SET
             markdown=excluded.markdown, model_used=excluded.model_used,
             generated_at=excluded.generated_at""",
        (law_id, markdown, llm.chat_model, now),
    )
    db.commit()
    return {"cached": False, "markdown": markdown, "model_used": llm.chat_model,
            "generated_at": now}
