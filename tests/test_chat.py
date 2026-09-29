# tests/test_chat.py — Agent 对话循环测试（mock LLM，不联网）
import pytest

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


def test_system_prompt_rules():
    assert "禁止编造" in SYSTEM_PROMPT
    assert "search_laws" in SYSTEM_PROMPT
