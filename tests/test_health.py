# tests/test_health.py — /api/health 增强字段测试（不联网，monkeypatch DATA_DIR）
import json

import pytest

from app import main as app_main
from app.db import get_db, init_db


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    # health() 与 db() 运行时读模块全局 DATA_DIR，patch 掉即可函数级测试
    monkeypatch.setattr(app_main, "DATA_DIR", data)
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
    return data


def test_health_unconfigured(data_dir):
    r = app_main.health()
    assert r["status"] == "ok"
    assert r["llm_configured"] is False
    assert r["embedding_configured"] is False
    assert r["laws"] == 1 and r["relations"] == 1


def test_health_configured(data_dir):
    (data_dir / "config.json").write_text(json.dumps({
        "api_base": "https://gw.example.com/v1", "api_key": "sk-test",
        "chat_model": "m1", "embedding_model": "e1",
    }), encoding="utf-8")
    r = app_main.health()
    assert r["llm_configured"] is True
    assert r["embedding_configured"] is True


def test_health_partial_config_counts_unconfigured(data_dir):
    # 只有 api_base，无 key/model → 判定未配置
    (data_dir / "config.json").write_text(json.dumps({
        "api_base": "https://gw.example.com/v1", "api_key": "",
    }), encoding="utf-8")
    r = app_main.health()
    assert r["llm_configured"] is False
