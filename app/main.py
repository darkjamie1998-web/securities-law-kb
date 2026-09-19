# app/main.py — FastAPI 入口：API 端点 + 静态页面挂载
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.chat import agent_chat
from app.config import load_config, save_config
from app.db import get_db, init_db
from app.llm import LLMClient, LLMError
from app.search import hybrid_search
from app.wiki import get_or_generate

app = FastAPI(title="证券法律法规知识库")

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"


def db() -> sqlite3.Connection:
    """每个请求用独立连接（SQLite 轻量，开销可忽略）。"""
    DATA_DIR.mkdir(exist_ok=True)
    conn = get_db(DATA_DIR / "knowledge.db")
    init_db(conn)
    return conn


def try_llm() -> LLMClient | None:
    """按当前配置构造 LLM 客户端；未配置时返回 None（检索退化为关键词）。"""
    try:
        return LLMClient(load_config(DATA_DIR))
    except LLMError:
        return None


# ---- 基础 ----
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    conn = db()
    by_level = {
        r["level"]: r["c"]
        for r in conn.execute(
            "SELECT level, count(*) AS c FROM laws GROUP BY level ORDER BY c DESC"
        )
    }
    last_sync = conn.execute(
        "SELECT source_name, finished_at, status FROM sync_log "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "laws": conn.execute("SELECT count(*) AS c FROM laws").fetchone()["c"],
        "articles": conn.execute("SELECT count(*) AS c FROM articles").fetchone()["c"],
        "embedded": conn.execute(
            "SELECT count(*) AS c FROM articles WHERE embedding IS NOT NULL"
        ).fetchone()["c"],
        "relations": conn.execute(
            "SELECT count(*) AS c FROM relations"
        ).fetchone()["c"],
        "by_level": by_level,
        "last_sync": dict(last_sync) if last_sync else None,
    }


# ---- 法规浏览 ----
@app.get("/api/laws")
def list_laws(
    level: str | None = None,
    topic: str | None = None,
    q: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    conn = db()
    where, params = [], []
    if level:
        where.append("level = ?")
        params.append(level)
    if status:
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("(title LIKE ? OR full_text LIKE ?)")
        params.extend([f"%{q}%"] * 2)
    if topic:
        where.append("topic_tags LIKE ?")
        params.append(f"%{topic}%")
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT count(*) AS c FROM laws {cond}", params
    ).fetchone()["c"]
    rows = conn.execute(
        f"""SELECT id, title, doc_number, issuer, level, topic_tags, issue_date,
            effective_date, status, source_name, source_url, updated_at
            FROM laws {cond} ORDER BY id DESC LIMIT ? OFFSET ?""",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [dict(r) for r in rows]}


@app.get("/api/laws/{law_id}")
def law_detail(law_id: int):
    conn = db()
    law = conn.execute(
        "SELECT * FROM laws WHERE id = ?", (law_id,)
    ).fetchone()
    if not law:
        raise HTTPException(404, "法规不存在")
    articles = conn.execute(
        "SELECT id, article_no, text FROM articles WHERE law_id = ? ORDER BY id",
        (law_id,),
    ).fetchall()
    wiki = conn.execute(
        "SELECT markdown, model_used, generated_at FROM wiki_pages WHERE law_id = ?",
        (law_id,),
    ).fetchone()
    return {
        "law": dict(law),
        "articles": [dict(a) for a in articles],
        "wiki": dict(wiki) if wiki else None,
    }


@app.get("/api/laws/{law_id}/relations")
def law_relations(law_id: int):
    """该法规法条的出向+入向关联。"""
    conn = db()
    law = conn.execute("SELECT id FROM laws WHERE id=?", (law_id,)).fetchone()
    if not law:
        raise HTTPException(404, "法规不存在")
    out = conn.execute(
        """SELECT r.rel_type, r.note, r.from_id, r.to_id,
                  fa.law_id AS from_law_id, la.law_id AS to_law_id,
                  lt.title AS to_law_title, la.article_no AS to_article_no,
                  ft.title AS from_law_title, fa.article_no AS from_article_no
           FROM relations r
           JOIN articles fa ON fa.id = r.from_id
           JOIN laws ft ON ft.id = fa.law_id
           JOIN articles la ON la.id = r.to_id
           JOIN laws lt ON lt.id = la.law_id
           WHERE fa.law_id = ?""",
        (law_id,),
    ).fetchall()
    inc = conn.execute(
        """SELECT r.rel_type, r.note, r.from_id, r.to_id,
                  fa.law_id AS from_law_id, la.law_id AS to_law_id,
                  lt.title AS to_law_title, la.article_no AS to_article_no,
                  ft.title AS from_law_title, fa.article_no AS from_article_no
           FROM relations r
           JOIN articles fa ON fa.id = r.from_id
           JOIN laws ft ON ft.id = fa.law_id
           JOIN articles la ON la.id = r.to_id
           JOIN laws lt ON lt.id = la.law_id
           WHERE la.law_id = ?""",
        (law_id,),
    ).fetchall()
    return {"outgoing": [dict(r) for r in out], "incoming": [dict(r) for r in inc]}


# ---- 检索 ----
@app.get("/api/search")
def search(q: str = Query(..., min_length=1), top_k: int = Query(10, ge=1, le=50)):
    conn = db()
    results = hybrid_search(conn, try_llm(), q, top_k)
    return {"query": q, "count": len(results), "results": results}


# ---- 对话（Agent 多轮检索） ----
class ChatRequest(BaseModel):
    messages: list[dict]  # [{"role": "user"|"assistant", "content": "..."}]


@app.post("/api/chat")
def chat(req: ChatRequest):
    conn = db()
    try:
        llm = LLMClient(load_config(DATA_DIR))
    except LLMError as e:
        raise HTTPException(400, f"大模型未配置：{e}")
    try:
        result = agent_chat(conn, llm, req.messages)
    except LLMError as e:
        raise HTTPException(502, f"大模型调用失败：{e}")
    return result


# ---- 模型配置 ----
class SettingsBody(BaseModel):
    api_base: str = ""
    api_key: str = ""
    chat_model: str = ""
    embedding_model: str = ""
    temperature: float = 0.1


def _mask(key: str) -> str:
    if not key or len(key) < 8:
        return "*" * len(key)
    return key[:3] + "****" + key[-4:]


@app.get("/api/settings")
def get_settings():
    cfg = load_config(DATA_DIR)
    return {
        "api_base": cfg["api_base"],
        "api_key_masked": _mask(cfg["api_key"]),
        "has_key": bool(cfg["api_key"]),
        "chat_model": cfg["chat_model"],
        "embedding_model": cfg["embedding_model"],
        "temperature": cfg["temperature"],
    }


@app.put("/api/settings")
def put_settings(body: SettingsBody):
    cfg = load_config(DATA_DIR)
    cfg.update({
        "api_base": body.api_base or cfg["api_base"],
        # api_key 传空字符串表示保留原值（前端只在用户输入新值时提交）
        "api_key": body.api_key or cfg["api_key"],
        "chat_model": body.chat_model or cfg["chat_model"],
        "embedding_model": body.embedding_model or cfg["embedding_model"],
        "temperature": body.temperature,
    })
    saved = save_config(DATA_DIR, cfg)
    return {"ok": True, "settings": {
        "api_base": saved["api_base"], "api_key_masked": _mask(saved["api_key"]),
        "chat_model": saved["chat_model"], "embedding_model": saved["embedding_model"],
        "temperature": saved["temperature"],
    }}


@app.post("/api/settings/test")
def test_settings(body: SettingsBody | None = None):
    """用当前（或提交的）配置发一次最小请求验证连通。"""
    cfg = load_config(DATA_DIR)
    if body and (body.api_base or body.api_key or body.chat_model):
        cfg.update({
            "api_base": body.api_base or cfg["api_base"],
            "api_key": body.api_key or cfg["api_key"],
            "chat_model": body.chat_model or cfg["chat_model"],
        })
    try:
        client = LLMClient(cfg)
        msg = client.chat(
            [{"role": "user", "content": "回复两个字：连通"}],
            model=cfg.get("chat_model") or None,
        )
        return {"ok": True, "reply": (msg.get("content") or "")[:50]}
    except LLMError as e:
        return {"ok": False, "error": str(e)[:300]}


# ---- wiki 解读 ----
@app.post("/api/laws/{law_id}/wiki")
def law_wiki(law_id: int, force: bool = False):
    conn = db()
    try:
        llm = LLMClient(load_config(DATA_DIR))
    except LLMError as e:
        raise HTTPException(400, f"大模型未配置：{e}")
    result = get_or_generate(conn, llm, law_id, force=force)
    if result.get("error"):
        raise HTTPException(404 if "不存在" in result["error"] else 502,
                            result["error"])
    return result


# ---- 静态 ----
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")
