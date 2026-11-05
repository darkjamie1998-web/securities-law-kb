# app/main.py — FastAPI 入口：应用工厂 + API 端点 + 静态页面挂载
import asyncio
import json
import logging
import queue
import re
import sqlite3
import threading
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.chat import ChatCancelled, agent_chat
from app.config import load_config, save_config
from app.db import get_db, init_db
from app.graph import build_graph_cached
from app.llm import LLMClient, LLMError
from app.search import (
    _TOPIC_NORMALIZE_SQL, _normalize_topic_text, hybrid_search_page, jieba_terms,
    normalize_article_no,
)
from app.seg_index import add_titles, ensure_fresh, ensure_schema
from app.wiki import get_or_generate

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
MAX_ARTICLE_PAGE = 1_000_000  # 限制 OFFSET，确保远小于 SQLite 有符号 64 位上限
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    messages: list[dict]  # [{"role": "user"|"assistant", "content": "..."}]
    law_id: int | None = None


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


def _normalize_reader_article_no(no: str) -> str:
    """reader 全串校验后复用全局条号归一化，兼容末尾说明括号。"""
    raw = str(no or "").strip()
    match = re.fullmatch(
        r"第([一二三四五六七八九十百千零〇\d]+)条"
        r"(?:之([一二三四五六七八九十百千零〇\d]+))?"
        r"(?:[（(][^（）()]+[）)])?", raw)
    if not match:
        return raw
    return normalize_article_no(raw)


def _reader_paging(page: str | None, page_size: str | None) -> tuple[int, int]:
    """仅在 reader 模式解析分页，旧详情继续忽略这些查询参数。"""
    try:
        page_value = 1 if page is None else int(page)
        size_value = 50 if page_size is None else int(page_size)
    except (TypeError, ValueError):
        raise HTTPException(422, "分页参数无效")
    if not 1 <= page_value <= MAX_ARTICLE_PAGE or not 1 <= size_value <= 100:
        raise HTTPException(422, "分页参数超出范围")
    return page_value, size_value


def _article_page(conn: sqlite3.Connection, law_id: int, page: int, page_size: int,
                  article_no: str | None = None, q: str | None = None) -> dict:
    """单部法规的分文分页；条号定位优先，提供条号时忽略文本筛选。"""
    index_rows = conn.execute(
        "SELECT id, article_no FROM articles WHERE law_id = ? ORDER BY id",
        (law_id,),
    ).fetchall()
    article_index = [dict(row) for row in index_rows]
    if not index_rows:
        law = conn.execute(
            "SELECT full_text FROM laws WHERE id = ?", (law_id,)).fetchone()
        if law and law["full_text"]:
            fallback = {"id": None, "article_no": "全文", "text": law["full_text"]}
            needle = q.casefold() if q else ""
            matched = (not q or needle in fallback["article_no"].casefold()
                       or needle in fallback["text"].casefold())
            total = 1 if article_no or matched else 0
            return {
                "articles": [fallback] if total and page == 1 else [],
                "article_index": [{"id": None, "article_no": "全文"}],
                "article_total": total,
                "article_page": page,
                "article_page_size": page_size,
                "target_article_id": None,
            }

    target_article_id = None
    if article_no:
        raw_no = str(article_no).strip()
        target_position = next((
            position for position, row in enumerate(index_rows)
            if str(row["article_no"] or "").strip() == raw_no
        ), None)
        if target_position is None:
            normalized_no = _normalize_reader_article_no(raw_no)
            target_position = next((
                position for position, row in enumerate(index_rows)
                if _normalize_reader_article_no(row["article_no"]) == normalized_no
            ), None)
        if target_position is not None:
            target_article_id = index_rows[target_position]["id"]
            page = target_position // page_size + 1

    where, params = ["law_id = ?"], [law_id]
    if not article_no and q:
        eq = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(r"(article_no LIKE ? ESCAPE '\' OR text LIKE ? ESCAPE '\')")
        params.extend([f"%{eq}%", f"%{eq}%"])
    cond = " AND ".join(where)
    total = conn.execute(
        f"SELECT count(*) AS c FROM articles WHERE {cond}", params
    ).fetchone()["c"]
    rows = conn.execute(
        f"""SELECT id, article_no, text FROM articles WHERE {cond}
            ORDER BY id LIMIT ? OFFSET ?""",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "articles": [dict(row) for row in rows],
        "article_index": article_index,
        "article_total": total,
        "article_page": page,
        "article_page_size": page_size,
        "target_article_id": target_article_id,
    }


def create_app(data_dir: Path | None = None) -> FastAPI:
    """应用工厂：测试可注入临时数据目录，生产默认 data/。"""
    data_dir = Path(data_dir) if data_dir else DATA_DIR

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = get_db(data_dir / "knowledge.db")
        init_db(conn)
        ensure_schema(conn)
        try:
            add_titles([r["title"] for r in conn.execute("SELECT title FROM laws")])
            ensure_fresh(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(title="证券法律法规知识库", lifespan=lifespan)
    app.state.data_dir = data_dir

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = get_db(data_dir / "knowledge.db")
        try:
            yield conn
        finally:
            conn.close()

    def try_llm() -> LLMClient | None:
        """按当前配置构造 LLM 客户端；未配置时返回 None（检索退化为关键词）。"""
        try:
            return LLMClient(load_config(data_dir))
        except LLMError:
            return None

    # ---- 基础 ----
    @app.get("/api/health")
    def health(conn: sqlite3.Connection = Depends(get_conn)):
        """健康检查 + 环境概况：运维/接手 Agent 一眼判断 LLM 是否配置、数据规模。"""
        cfg = load_config(data_dir)
        return {
            "status": "ok",
            "llm_configured": bool(cfg["api_base"] and cfg["api_key"] and cfg["chat_model"]),
            "embedding_configured": bool(cfg["embedding_model"]),
            "laws": conn.execute("SELECT count(*) FROM laws").fetchone()[0],
            "relations": conn.execute("SELECT count(*) FROM relations").fetchone()[0],
        }

    @app.get("/api/stats")
    def stats(conn: sqlite3.Connection = Depends(get_conn)):
        active_where = (
            "duplicate_of IS NULL AND superseded_by IS NULL "
            "AND (quality_flag IS NULL OR quality_flag NOT IN ('duplicate','empty_text'))"
        )
        by_level = {
            r["level"]: r["c"]
            for r in conn.execute(
                f"SELECT level, count(*) AS c FROM laws WHERE {active_where} "
                "GROUP BY level ORDER BY c DESC"
            )
        }
        by_status = {
            r["status"]: r["c"]
            for r in conn.execute(
                f"SELECT status, count(*) AS c FROM laws WHERE {active_where} "
                "AND status IS NOT NULL AND trim(status) != '' "
                "GROUP BY status ORDER BY c DESC"
            )
        }
        by_source = {
            r["source_name"]: r["c"]
            for r in conn.execute(
                f"SELECT source_name, count(*) AS c FROM laws WHERE {active_where} "
                "AND source_name IS NOT NULL AND trim(source_name) != '' "
                "GROUP BY source_name ORDER BY c DESC, source_name"
            )
        }
        topic_counts = Counter(
            tag
            for r in conn.execute(
                f"SELECT topic_tags FROM laws WHERE {active_where} "
                "AND topic_tags IS NOT NULL"
            )
            for tag in set(_normalize_topic_text(r["topic_tags"]).split(","))
            if tag
        )
        topics = [
            {"name": name, "count": count}
            for name, count in sorted(
                topic_counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        last_sync = conn.execute(
            "SELECT source_name, finished_at, status FROM sync_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_success_sync = conn.execute(
            "SELECT * FROM sync_log WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_source_syncs = conn.execute(
            "SELECT * FROM sync_log WHERE id IN ("
            "SELECT MAX(id) FROM sync_log WHERE source_name IS NOT NULL "
            "GROUP BY source_name) ORDER BY source_name"
        ).fetchall()
        latest_source_successes = {
            r["source_name"]: r
            for r in conn.execute(
                "SELECT * FROM sync_log WHERE status = 'ok' AND id IN ("
                "SELECT MAX(id) FROM sync_log WHERE status = 'ok' "
                "AND source_name IS NOT NULL GROUP BY source_name)"
            )
        }
        sources = {}
        for row in latest_source_syncs:
            success = latest_source_successes.get(row["source_name"])
            sources[row["source_name"]] = {
                "last_sync": dict(row),
                "last_success_sync": dict(success) if success else None,
            }
        searchable_where = (
            "l.duplicate_of IS NULL AND l.superseded_by IS NULL "
            "AND (l.quality_flag IS NULL "
            "OR l.quality_flag NOT IN ('duplicate','empty_text'))"
        )
        searchable_from = (
            "FROM articles a JOIN laws l ON l.id = a.law_id "
            f"WHERE {searchable_where}"
        )
        searchable_articles = conn.execute(
            f"SELECT count(*) AS c {searchable_from}"
        ).fetchone()["c"]
        embedded_searchable = conn.execute(
            f"SELECT count(*) AS c {searchable_from} AND a.embedding IS NOT NULL"
        ).fetchone()["c"]
        content_updated_at = conn.execute(
            f"SELECT MAX(COALESCE(updated_at, created_at)) AS value FROM laws "
            f"WHERE {active_where}"
        ).fetchone()["value"]
        cfg = load_config(data_dir)
        embedding_ready = all(
            cfg[key] for key in ("api_base", "api_key", "embedding_model"))
        return {
            "laws": conn.execute(
                f"SELECT count(*) AS c FROM laws WHERE {active_where}"
            ).fetchone()["c"],
            "total_laws": conn.execute("SELECT count(*) AS c FROM laws").fetchone()["c"],
            "articles": conn.execute("SELECT count(*) AS c FROM articles").fetchone()["c"],
            "embedded": conn.execute(
                "SELECT count(*) AS c FROM articles WHERE embedding IS NOT NULL"
            ).fetchone()["c"],
            "relations": conn.execute(
                "SELECT count(*) AS c FROM relations"
            ).fetchone()["c"],
            "by_level": by_level,
            "last_sync": dict(last_sync) if last_sync else None,
            "last_success_sync": dict(last_success_sync) if last_success_sync else None,
            "content_updated_at": content_updated_at,
            "sources": sources,
            "by_status": by_status,
            "by_source": by_source,
            "topics": topics,
            "searchable_articles": searchable_articles,
            "embedded_searchable": embedded_searchable,
            "mode": (
                "hybrid" if embedding_ready and embedded_searchable > 0
                else "keyword"
            ),
        }

    # ---- 法规浏览 ----
    @app.get("/api/laws")
    def list_laws(
        level: str | None = None,
        topic: str | None = None,
        q: str | None = None,
        status: str | None = None,
        source: str | None = None,
        sort: str = Query("id", pattern="^(id|updated)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        where, params = [], []
        # 默认只展示现行、非重复、非空数据的法规
        where.append("duplicate_of IS NULL")
        where.append("superseded_by IS NULL")
        where.append("(quality_flag IS NULL OR quality_flag NOT IN ('duplicate','empty_text'))")
        if level:
            where.append("level = ?")
            params.append(level)
        if status:
            where.append("status = ?")
            params.append(status)
        if source:
            where.append("source_name = ?")
            params.append(source)
        if q:
            # 标题 LIKE + 分词索引命中的法规集合；避免对 full_text 做全表 LIKE。
            eq = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            cond_parts = [r"title LIKE ? ESCAPE '\'"]
            params.append(f"%{eq}%")
            seg_terms = jieba_terms(q)
            seg_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles_seg'"
            ).fetchone()
            if seg_terms and seg_table:
                expr = " OR ".join(
                    f'"{t.replace(chr(34), chr(34) * 2)}"' for t in seg_terms)
                cond_parts.append(
                    """EXISTS (
                        SELECT 1 FROM articles_seg
                        JOIN articles a ON a.id = articles_seg.rowid
                        WHERE a.law_id = laws.id AND articles_seg MATCH ?
                    )""")
                params.append(expr)
            where.append("(" + " OR ".join(cond_parts) + ")")
        if topic is not None:
            eq_t = _normalize_topic_text(topic)
            if eq_t:
                where.append(
                    "instr(',' || " + _TOPIC_NORMALIZE_SQL + " || ',', ?) > 0")
                params.append(f",{eq_t},")
            else:
                where.append("0")
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT count(*) AS c FROM laws {cond}", params
        ).fetchone()["c"]
        order_by = (
            "COALESCE(updated_at, created_at) DESC, id DESC"
            if sort == "updated" else "id DESC"
        )
        rows = conn.execute(
            f"""SELECT id, title, doc_number, issuer, level, topic_tags, issue_date,
                effective_date, status, source_name, source_url, updated_at
                FROM laws {cond} ORDER BY {order_by} LIMIT ? OFFSET ?""",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
        return {"total": total, "page": page, "page_size": page_size,
                "items": [dict(r) for r in rows]}

    @app.get("/api/laws/{law_id}")
    def law_detail(law_id: int, mode: str | None = None,
                   page: str | None = None, page_size: str | None = None,
                   article_no: str | None = None,
                   conn: sqlite3.Connection = Depends(get_conn)):
        if mode == "reader":
            law = conn.execute(
                """SELECT id, title, doc_number, issuer, level, topic_tags, issue_date,
                    published_at, effective_date, status, source_name, source_url,
                    content_hash, created_at, updated_at, quality_flag, duplicate_of,
                    superseded_by, version_label FROM laws WHERE id = ?""",
                (law_id,),
            ).fetchone()
            if not law:
                raise HTTPException(404, "法规不存在")
            page_value, size_value = _reader_paging(page, page_size)
            wiki = conn.execute(
                "SELECT markdown, model_used, generated_at FROM wiki_pages WHERE law_id = ?",
                (law_id,),
            ).fetchone()
            return {
                "law": dict(law),
                **_article_page(conn, law_id, page_value, size_value, article_no),
                "wiki": dict(wiki) if wiki else None,
            }
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

    @app.get("/api/laws/{law_id}/articles")
    def law_articles(law_id: int, page: int = Query(1, ge=1, le=MAX_ARTICLE_PAGE),
                     page_size: int = Query(50, ge=1, le=100),
                     article_no: str | None = None, q: str | None = None,
                     conn: sqlite3.Connection = Depends(get_conn)):
        law = conn.execute("SELECT id FROM laws WHERE id = ?", (law_id,)).fetchone()
        if not law:
            raise HTTPException(404, "法规不存在")
        return _article_page(conn, law_id, page, page_size, article_no, q)

    @app.get("/api/laws/{law_id}/relations")
    def law_relations(law_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        """该法规法条的出向+入向关联。"""
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
    def search(q: str = Query(..., min_length=1),
               top_k: int | None = Query(None, ge=1, le=50),
               page: int = Query(1, ge=1),
               page_size: int = Query(10, ge=1, le=100),
               level: str | None = None,
               status: str | None = None,
               topic: str | None = None,
               source: str | None = None,
               conn: sqlite3.Connection = Depends(get_conn)):
        effective_size = top_k if top_k is not None else page_size
        page_data = hybrid_search_page(
            conn, try_llm(), q, page=page, page_size=effective_size,
            filters={"level": level, "status": status,
                     "topic": topic, "source": source},
        )
        return {
            "query": q, "count": len(page_data["results"]),
            "results": page_data["results"], "total": page_data["total"],
            "page": page_data["page"], "page_size": page_data["page_size"],
            "has_more": page_data["has_more"], "mode": page_data["mode"],
        }

    # ---- 对话（Agent 多轮检索） ----
    @app.post("/api/chat")
    def chat(req: ChatRequest, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            llm = LLMClient(load_config(data_dir))
        except LLMError as e:
            raise HTTPException(400, f"大模型未配置：{e}")
        try:
            result = agent_chat(conn, llm, req.messages, law_id=req.law_id)
        except LLMError as e:
            raise HTTPException(502, f"大模型调用失败：{e}")
        return result

    @app.post("/api/chat/stream")
    def chat_stream(req: ChatRequest):
        try:
            llm = LLMClient(load_config(data_dir))
        except LLMError as e:
            raise HTTPException(400, f"大模型未配置：{e}")

        events = queue.Queue()
        cancel_event = threading.Event()

        def worker():
            conn = None
            try:
                # SQLite 连接不能跨线程共享，必须在 worker 内创建并关闭。
                conn = get_db(data_dir / "knowledge.db")
                result = agent_chat(
                    conn, llm, req.messages, progress=events.put,
                    cancel_event=cancel_event, law_id=req.law_id)
                events.put({"type": "result", "data": result})
            except ChatCancelled:
                events.put({"type": "cancelled"})
            except LLMError as e:
                events.put({"type": "error", "error": f"大模型调用失败：{e}"})
            except Exception as e:
                logger.exception("流式对话 worker 异常：%s", type(e).__name__)
                events.put({
                    "type": "error",
                    "error": f"对话处理失败（{type(e).__name__}）",
                })
            finally:
                if conn is not None:
                    conn.close()

        worker_thread = threading.Thread(
            target=worker, name="chat-stream", daemon=True)
        worker_thread.start()

        async def generate():
            try:
                while True:
                    try:
                        # 带超时的线程轮询让 ASGI 取消及时进入 finally，
                        # 辅助线程也不会因永久 queue.get 阻塞而泄漏。
                        event = await asyncio.to_thread(events.get, True, 0.1)
                    except queue.Empty:
                        continue
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                    if event["type"] in {"result", "error", "cancelled"}:
                        break
            finally:
                # 断开连接只设置协作标记；正在执行的 LLM HTTP 不会被强杀。
                # worker 是 daemon，正常终止本已结束，断连时无需等待它退出。
                cancel_event.set()

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    # ---- 模型配置 ----
    @app.get("/api/settings")
    def get_settings():
        cfg = load_config(data_dir)
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
        cfg = load_config(data_dir)
        cfg.update({
            "api_base": body.api_base or cfg["api_base"],
            # api_key 传空字符串表示保留原值（前端只在用户输入新值时提交）
            "api_key": body.api_key or cfg["api_key"],
            "chat_model": body.chat_model or cfg["chat_model"],
            "embedding_model": body.embedding_model or cfg["embedding_model"],
            "temperature": body.temperature,
        })
        saved = save_config(data_dir, cfg)
        return {"ok": True, "settings": {
            "api_base": saved["api_base"], "api_key_masked": _mask(saved["api_key"]),
            "chat_model": saved["chat_model"], "embedding_model": saved["embedding_model"],
            "temperature": saved["temperature"],
        }}

    @app.post("/api/settings/test")
    def test_settings(body: SettingsBody | None = None):
        """用当前（或提交的）配置发一次最小请求验证连通。"""
        cfg = load_config(data_dir)
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

    # ---- 知识图谱 ----
    @app.get("/api/graph")
    def graph(conn: sqlite3.Connection = Depends(get_conn)):
        """法规级全景图谱（径向分层 Canvas 渲染的数据源）。"""
        return build_graph_cached(conn)

    # ---- wiki 解读 ----
    @app.post("/api/laws/{law_id}/wiki")
    def law_wiki(law_id: int, force: bool = False,
                 conn: sqlite3.Connection = Depends(get_conn)):
        try:
            llm = LLMClient(load_config(data_dir))
        except LLMError as e:
            raise HTTPException(400, f"大模型未配置：{e}")
        try:
            result = get_or_generate(conn, llm, law_id, force=force)
        except LLMError as e:
            raise HTTPException(502, f"大模型调用失败：{e}")
        if result.get("error"):
            raise HTTPException(404 if "不存在" in result["error"] else 502,
                                result["error"])
        return result

    # ---- 静态 ----
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()
