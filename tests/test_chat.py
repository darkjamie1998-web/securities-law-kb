# tests/test_chat.py — Agent 对话循环测试（mock LLM，不联网）
import threading

import pytest

import app.chat as chat_module
from app.chat import agent_chat, _extract_citations, SYSTEM_PROMPT
from app.db import get_db, init_db


@pytest.fixture()
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("中华人民共和国证券法", "法律", "h1"),
    )
    conn.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (cur.lastrowid, "第五十条",
         "禁止证券交易内幕信息的知情人利用内幕信息从事证券交易活动。"),
    )
    conn.commit()
    return conn


class ScriptedLLM:
    """按脚本依次返回：第一轮调工具，第二轮给最终回答。"""

    embedding_model = "mock-embed"  # hybrid_search 据此判断是否走向量路

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        return self.script.pop(0)

    def embed(self, texts, batch=64):
        return [[0.0] * 4 for _ in texts]


TOOL_MSG = {
    "role": "assistant",
    "content": None,
    "tool_calls": [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "search_laws", "arguments": '{"query": "内幕交易 禁止"}'},
    }],
}
FINAL_MSG = {
    "role": "assistant",
    "content": "根据检索结果，禁止内幕交易【中华人民共和国证券法·第五十条】。",
}


def test_agent_loop_tool_then_answer(db):
    llm = ScriptedLLM([TOOL_MSG, FINAL_MSG])
    r = agent_chat(db, llm, [{"role": "user", "content": "内幕交易有什么规定？"}])
    assert r["rounds"] == 2
    assert r["searches"] == ["内幕交易 禁止"]
    assert "内幕交易" in r["content"]
    assert len(r["citations"]) == 1
    assert r["citations"][0]["article_no"] == "第五十条"


def test_agent_loop_no_tool_direct_answer(db):
    llm = ScriptedLLM([FINAL_MSG])
    r = agent_chat(db, llm, [{"role": "user", "content": "问题"}])
    assert r["rounds"] == 1
    assert r["searches"] == []


def test_agent_loop_stops_at_max_rounds(db):
    llm = ScriptedLLM([TOOL_MSG] * 10)
    r = agent_chat(db, llm, [{"role": "user", "content": "问题"}], max_rounds=3)
    assert r["rounds"] == 3


def test_agent_chat_emits_ordered_progress_events(db):
    events = []
    llm = ScriptedLLM([TOOL_MSG, FINAL_MSG])

    agent_chat(db, llm, [{"role": "user", "content": "内幕交易"}],
               progress=events.append)

    assert events == [
        {"type": "stage", "stage": "thinking", "round": 1},
        {"type": "stage", "stage": "model_done", "round": 1},
        {"type": "stage", "stage": "searching", "round": 1,
         "query": "内幕交易 禁止"},
        {"type": "stage", "stage": "search_done", "round": 1,
         "query": "内幕交易 禁止"},
        {"type": "stage", "stage": "thinking", "round": 2},
        {"type": "stage", "stage": "model_done", "round": 2},
        {"type": "stage", "stage": "citing", "round": 2},
    ]


def test_cancel_after_tool_prevents_next_llm_call(db, monkeypatch):
    cancel_event = threading.Event()

    def cancel_after_search(*args, **kwargs):
        cancel_event.set()
        return []

    monkeypatch.setattr(chat_module, "hybrid_search", cancel_after_search)
    llm = ScriptedLLM([TOOL_MSG, FINAL_MSG])

    with pytest.raises(chat_module.ChatCancelled):
        agent_chat(db, llm, [{"role": "user", "content": "内幕交易"}],
                   cancel_event=cancel_event)

    assert llm.calls == 1


def test_law_id_scopes_search_and_model_context(db, monkeypatch):
    captured = {"filters": [], "messages": []}

    def fake_search(db, llm, query, top_k=10, filters=None):
        captured["filters"].append(filters)
        return []

    class CaptureLLM(ScriptedLLM):
        def chat(self, messages, tools=None, **kw):
            captured["messages"].append([dict(m) for m in messages])
            return super().chat(messages, tools, **kw)

    monkeypatch.setattr(chat_module, "hybrid_search", fake_search)
    llm = CaptureLLM([TOOL_MSG, FINAL_MSG])
    result = agent_chat(db, llm, [{"role": "user", "content": "问题"}], law_id=7)

    assert captured["filters"] == [{"law_id": 7}]
    assert result["scope"] == {"law_id": 7}
    scoped_context = captured["messages"][0][1]
    assert scoped_context["role"] == "system"
    assert "law_id=7" in scoped_context["content"]
    assert "不得" in scoped_context["content"]
    assert "全库" in scoped_context["content"]

    unscoped_llm = CaptureLLM([TOOL_MSG, FINAL_MSG])
    unscoped = agent_chat(db, unscoped_llm, [{"role": "user", "content": "问题"}])
    assert captured["filters"][-1] is None
    assert unscoped["scope"] == {"law_id": None}
    assert len(captured["messages"][-2]) == 2  # system + user，不追加限定上下文


def test_legacy_positional_max_rounds_and_result_fields(db):
    llm = ScriptedLLM([FINAL_MSG])

    result = agent_chat(db, llm, [{"role": "user", "content": "问题"}], 1)

    assert result["content"] == FINAL_MSG["content"]
    assert result["searches"] == [] and result["rounds"] == 1
    assert len(result["citations"]) == 1
    assert result["scope"] == {"law_id": None}


def test_tool_result_fed_back(db):
    """工具执行结果必须作为 tool 消息回填给 LLM。"""
    captured = {}

    class CaptureLLM(ScriptedLLM):
        def chat(self, messages, tools=None, **kw):
            captured[len(messages)] = [
                m.get("role") for m in messages
            ]
            return super().chat(messages, tools, **kw)

    llm = CaptureLLM([TOOL_MSG, FINAL_MSG])
    agent_chat(db, llm, [{"role": "user", "content": "内幕交易"}])
    # 第二次调用时消息里应包含 tool role
    assert any("tool" in roles for roles in captured.values())


def test_extract_citations_no_match(db):
    assert _extract_citations(db, "没有引用格式的回答") == []


def test_extract_citations_includes_source_url(db):
    db.execute("UPDATE laws SET source_url='https://example.com/law'")
    db.commit()
    citations = _extract_citations(db, "【中华人民共和国证券法·第五十条】")
    assert citations and citations[0]["source_url"] == "https://example.com/law"


def test_extract_citations_normalizes_arabic_article_no(db):
    citations = _extract_citations(db, "【证券法·第50条】")
    assert citations and citations[0]["article_no"] == "第五十条"


def test_extract_citations_matches_compound_and_arabic_stored_articles(db):
    law_id = db.execute("SELECT id FROM laws").fetchone()[0]
    for no in ("第五十条之一", "第50条之二"):
        db.execute(
            "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
            (law_id, no, "复合条号正文。"))
    db.commit()

    citations = _extract_citations(
        db, "【证券法·第50条之1；证券法·第50条之二；证券法·第50条（说明）】")

    assert [c["article_no"] for c in citations] == [
        "第五十条之一", "第五十条之二", "第五十条"]
    assert all(c["article_id"] is not None for c in citations)
    assert len({c["article_id"] for c in citations}) == 3


def test_extract_citations_compound_never_falls_back_to_base_article(db):
    citations = _extract_citations(db, "【证券法·第50条之3】")
    assert len(citations) == 1
    assert citations[0]["law_id"] is not None
    assert citations[0]["article_id"] is None
    assert citations[0]["article_no"] == "第五十条之三"


def test_extract_citations_title_with_doc_number(db):
    # 正文常写《法规名》（证监会令第X号），数据库标题可能带【证监会令第X号】前缀
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("【证监会令第222号】公司债券发行与交易管理办法", "部门规章", "h222"))
    law_id = cur.lastrowid
    db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第十一条", "发行公司债券，可以附认股权、可转换成相关股票等条款。"))
    db.commit()
    citations = _extract_citations(
        db, "【《公司债券发行与交易管理办法》（证监会令第222号）·第十一条】")
    assert len(citations) == 1
    assert citations[0]["law_id"] == law_id
    assert citations[0]["article_id"] is not None


def test_extract_citations_keeps_law_only_for_mixed_no(db):
    # 第19十一条 是模型笔误，不能误解析成第十一条；保留法规但不定条号
    citations = _extract_citations(db, "【证券法·第19十一条】")
    assert len(citations) == 1
    assert citations[0]["law_id"] is not None
    assert citations[0]["article_id"] is None


def test_extract_citations_keeps_unmatched_law(db):
    citations = _extract_citations(db, "【刑法·第180条】")
    assert len(citations) == 1
    assert citations[0]["law_id"] is None
    assert citations[0]["article_no"] == "第一百八十条"


def test_extract_citations_keeps_law_without_article(db):
    citations = _extract_citations(db, "【证券法·第999条】")
    assert len(citations) == 1
    assert citations[0]["law_id"] is not None
    assert citations[0]["article_id"] is None


def test_extract_citations_splits_multiple_refs(db):
    law_id = db.execute("SELECT id FROM laws").fetchone()[0]
    db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第一百九十一条", "证券交易内幕信息知情人从事内幕交易的处罚。"))
    db.commit()
    citations = _extract_citations(
        db, "【证券法·第50条；证券法·第191条】")
    assert [c["article_no"] for c in citations] == ["第五十条", "第一百九十一条"]


def test_extract_citations_splits_comma_articles(db):
    law_id = db.execute("SELECT id FROM laws").fetchone()[0]
    db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第一百九十一条", "证券交易内幕信息知情人从事内幕交易的处罚。"))
    db.commit()
    citations = _extract_citations(db, "【证券法·第50条、第191条】")
    assert [c["article_no"] for c in citations] == ["第五十条", "第一百九十一条"]


def test_extract_citations_whole_law(db):
    citations = _extract_citations(db, "【证券法·全文】")
    assert len(citations) == 1
    assert citations[0]["law_id"] is not None
    assert citations[0]["article_id"] is None
    assert citations[0]["article_no"] == "全文"


def test_extract_citations_matches_explicit_modification_decision(db):
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("【证监会令第144号】关于修改《证券发行与承销管理办法》的决定",
         "部门规章", "h144"))
    db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (cur.lastrowid, "第十三条", "修改后的内容。"))
    db.commit()
    citations = _extract_citations(
        db, "【关于修改《证券发行与承销管理办法》的决定（证监会令第144号）·第十三条】")
    assert len(citations) == 1
    assert citations[0]["law_id"] is not None


def test_extract_citations_rejects_modified_original(db):
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("【证监会令第144号】关于修改《证券发行与承销管理办法》的决定",
         "部门规章", "h144"))
    db.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (cur.lastrowid, "第十三条", "修改后的内容。"))
    db.commit()
    # 库内只有修改决定、没有原办法全文时，不应把原办法引用错误指向修改决定
    citations = _extract_citations(db, "【证券发行与承销管理办法·第十三条】")
    assert len(citations) == 1
    assert citations[0]["law_id"] is None


def test_extract_citations_excludes_duplicate_law(db):
    law_id = db.execute("SELECT id FROM laws").fetchone()[0]
    db.execute(
        "INSERT INTO laws(title, level, content_hash, duplicate_of, quality_flag) "
        "VALUES('中华人民共和国证券法','法律','h2',?,'duplicate')", (law_id,))
    db.commit()
    citations = _extract_citations(db, "【中华人民共和国证券法·第五十条】")
    assert citations and citations[0]["law_id"] == law_id


def test_system_prompt_rules():
    assert "禁止编造" in SYSTEM_PROMPT
    assert "search_laws" in SYSTEM_PROMPT
