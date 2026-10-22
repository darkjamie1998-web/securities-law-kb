# tests/test_health.py — /api/health 增强字段测试（应用工厂 + 临时库）
import json

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.db import get_db, init_db


@pytest.fixture()
def client(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conn = get_db(data / "knowledge.db")
    init_db(conn)
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
        ("中华人民共和国证券法", "法律", "h1"))
    law_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第一条", "内容"))
    art_id = cur.lastrowid
    conn.execute(
        "INSERT INTO relations(from_id, to_id, rel_type, note) VALUES(?,?,?,?)",
        (art_id, art_id, "引用", "n"))
    conn.commit()
    conn.close()
    app = app_main.create_app(data)
    with TestClient(app) as c:
        yield c


def _write_config(client, body):
    (client.app.state.data_dir / "config.json").write_text(
        json.dumps(body), encoding="utf-8")


def test_health_unconfigured(client):
    r = client.get("/api/health").json()
    assert r["status"] == "ok"
    assert r["llm_configured"] is False
    assert r["embedding_configured"] is False
    assert r["laws"] == 1 and r["relations"] == 1


def test_health_configured(client):
    _write_config(client, {
        "api_base": "https://gw.example.com/v1", "api_key": "sk-test",
        "chat_model": "m1", "embedding_model": "e1",
    })
    r = client.get("/api/health").json()
    assert r["llm_configured"] is True
    assert r["embedding_configured"] is True


def test_health_partial_config_counts_unconfigured(client):
    # 只有 api_base，无 key/model → 判定未配置
    _write_config(client, {
        "api_base": "https://gw.example.com/v1", "api_key": "",
    })
    r = client.get("/api/health").json()
    assert r["llm_configured"] is False
