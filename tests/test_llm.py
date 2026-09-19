# tests/test_llm.py — LLM 客户端测试（mock httpx，不联网）
import json

import pytest

from app.llm import LLMClient, LLMError, cosine

CFG = {
    "api_base": "http://fake/v1",
    "api_key": "sk-test",
    "chat_model": "test-chat",
    "embedding_model": "test-embed",
    "temperature": 0.1,
}


class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self.payload


def test_chat_builds_correct_request(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)
        return FakeResp(payload={
            "choices": [{"message": {"role": "assistant", "content": "你好"}}]
        })

    monkeypatch.setattr("httpx.post", fake_post)
    client = LLMClient(CFG)
    msg = client.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "你好"
    assert captured["url"] == "http://fake/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"]["model"] == "test-chat"
    assert captured["body"]["temperature"] == 0.1


def test_chat_error_raises(monkeypatch):
    monkeypatch.setattr("httpx.post",
                        lambda *a, **k: FakeResp(status_code=401, text="unauthorized"))
    client = LLMClient(CFG)
    with pytest.raises(LLMError, match="401"):
        client.chat([{"role": "user", "content": "hi"}])


def test_missing_config_raises():
    with pytest.raises(LLMError):
        LLMClient({"api_base": "", "api_key": ""})


def test_embed_batches_and_orders(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["input"])
        # 故意乱序返回，验证按 index 排序
        n = len(json["input"])
        data = [{"index": n - 1 - i, "embedding": [float(i)]} for i in range(n)]
        return FakeResp(payload={"data": data})

    monkeypatch.setattr("httpx.post", fake_post)
    client = LLMClient(CFG)
    vecs = client.embed(["a", "b", "c"], batch=2)
    # 批1(n=2): data=[{1,[0]},{0,[1]}] → 排序后 [1.0, 0.0]；批2(n=1): [0.0]
    assert [v[0] for v in vecs] == [1.0, 0.0, 0.0]
    assert len(calls) == 2


def test_cosine():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine([], []) == 0.0
