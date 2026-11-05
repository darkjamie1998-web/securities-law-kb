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


@pytest.fixture()
def stats_client(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conn = get_db(data / "knowledge.db")
    init_db(conn)
    laws = [
        ("现行法规A", "法律", "发行， 交易,发行, ,信息披露", "现行有效",
         "证监会", "s1", "2026-01-01", "2026-02-01", None, None, None),
        ("现行法规B", "行政法规", "交易,上市", "试行", "上交所", "s2",
         "2026-04-01", None, None, None, None),
        ("重复法规", "法律", "重复", "现行有效", "重复源", "s3",
         "2026-06-01", "2026-06-02", 1, None, "duplicate"),
        ("空法规", "法律", "空", "现行有效", "空源", "s4",
         "2026-07-01", "2026-07-02", None, None, "empty_text"),
        ("旧版法规", "法律", "旧", "已修订", "旧源", "s5",
         "2026-08-01", "2026-08-02", None, 1, None),
    ]
    conn.executemany(
        "INSERT INTO laws(title, level, topic_tags, status, source_name, "
        "content_hash, created_at, updated_at, duplicate_of, superseded_by, "
        "quality_flag) VALUES(?,?,?,?,?,?,?,?,?,?,?)", laws)
    articles = [
        (1, "第一条", "有效且已向量化", "v1"),
        (1, "第二条", "有效未向量化", None),
        (2, "第一条", "另一部有效法规", None),
        (3, "第一条", "重复法规法条", "v-duplicate"),
        (4, "第一条", "空法规法条", "v-empty"),
        (5, "第一条", "旧版法规法条", "v-old"),
    ]
    conn.executemany(
        "INSERT INTO articles(law_id, article_no, text, embedding) VALUES(?,?,?,?)",
        articles)
    conn.execute(
        "INSERT INTO relations(from_id, to_id, rel_type) VALUES(1, 1, '引用')")
    logs = [
        ("北交所", "2025-12-01T09:00:00", "2025-12-01T10:00:00", 0, 0,
         "failed", "尚无成功同步"),
        ("证监会", "2026-01-01T09:00:00", "2026-01-02T09:00:00", 2, 1,
         "ok", "首次成功"),
        ("上交所", "2026-01-02T09:00:00", "2026-01-03T09:00:00", 3, 0,
         "ok", "最近成功"),
        ("证监会", "2026-01-03T09:00:00", "2026-01-04T09:00:00", 0, 0,
         "failed", "最近尝试失败"),
        ("上交所", "2026-01-04T09:00:00", "2026-01-05T09:00:00", 0, 0,
         "failed", "全局最近尝试"),
    ]
    conn.executemany(
        "INSERT INTO sync_log(source_name, started_at, finished_at, new_count, "
        "updated_count, status, detail) VALUES(?,?,?,?,?,?,?)", logs)
    conn.commit()
    conn.close()
    app = app_main.create_app(data)
    with TestClient(app) as c:
        yield c


def test_stats_sync_status_distinguishes_attempt_from_success(stats_client):
    stats = stats_client.get("/api/stats").json()

    assert stats["last_sync"] == {
        "source_name": "上交所", "finished_at": "2026-01-05T09:00:00",
        "status": "failed",
    }
    assert stats["last_success_sync"]["source_name"] == "上交所"
    assert stats["last_success_sync"]["finished_at"] == "2026-01-03T09:00:00"
    assert stats["last_success_sync"]["status"] == "ok"
    assert set(stats["sources"]) == {"证监会", "上交所", "北交所"}
    assert stats["sources"]["北交所"]["last_sync"]["status"] == "failed"
    assert stats["sources"]["北交所"]["last_success_sync"] is None
    source = stats["sources"]["证监会"]
    assert source["last_sync"]["status"] == "failed"
    assert source["last_sync"]["finished_at"] == "2026-01-04T09:00:00"
    assert source["last_success_sync"]["status"] == "ok"
    assert source["last_success_sync"]["finished_at"] == "2026-01-02T09:00:00"
    assert source["last_success_sync"]["started_at"] == "2026-01-01T09:00:00"
    assert source["last_success_sync"]["new_count"] == 2
    assert source["last_success_sync"]["updated_count"] == 1
    assert source["last_success_sync"]["detail"] == "首次成功"


def test_stats_searchable_counts_and_filters_use_current_laws(stats_client):
    stats = stats_client.get("/api/stats").json()

    # 旧字段保留原口径；新增可检索统计排除重复、空法规和旧版本。
    assert stats["laws"] == 2
    assert stats["total_laws"] == 5
    assert stats["articles"] == 6
    assert stats["embedded"] == 4
    assert stats["relations"] == 1
    assert stats["by_level"] == {"法律": 1, "行政法规": 1}
    assert stats["searchable_articles"] == 3
    assert stats["embedded_searchable"] == 1
    assert stats["content_updated_at"] == "2026-04-01"
    assert stats["by_status"] == {"现行有效": 1, "试行": 1}
    assert stats["by_source"] == {"证监会": 1, "上交所": 1}
    assert stats["topics"][0] == {"name": "交易", "count": 2}
    assert {item["name"]: item["count"] for item in stats["topics"]} == {
        "发行": 1, "交易": 2, "信息披露": 1, "上市": 1,
    }
    topic_counts = [item["count"] for item in stats["topics"]]
    assert topic_counts == sorted(topic_counts, reverse=True)


def test_stats_mode_requires_complete_embedding_credentials(stats_client):
    assert stats_client.get("/api/stats").json()["embedded_searchable"] == 1
    incomplete_configs = [
        {"api_base": "", "api_key": "sk-test", "embedding_model": "e1"},
        {"api_base": "https://gw.example.com/v1", "api_key": "",
         "embedding_model": "e1"},
        {"api_base": "https://gw.example.com/v1", "api_key": "sk-test",
         "embedding_model": ""},
    ]
    for config in incomplete_configs:
        _write_config(stats_client, config)
        assert stats_client.get("/api/stats").json()["mode"] == "keyword"

    _write_config(stats_client, {
        "api_base": "https://gw.example.com/v1", "api_key": "sk-test",
        "embedding_model": "e1",
    })
    assert stats_client.get("/api/stats").json()["mode"] == "hybrid"


def test_stats_defaults_without_sync_or_searchable_vectors(client):
    _write_config(client, {
        "api_base": "https://gw.example.com/v1", "api_key": "sk-test",
        "embedding_model": "e1",
    })
    stats = client.get("/api/stats").json()

    assert stats["searchable_articles"] == 1
    assert stats["embedded_searchable"] == 0
    assert stats["mode"] == "keyword"
    assert stats["last_sync"] is None
    assert stats["last_success_sync"] is None
    assert stats["sources"] == {}
    assert stats["content_updated_at"] is None
    assert stats["by_source"] == {}
    assert stats["topics"] == []


def test_stats_status_facets_exclude_null_and_blank_values(client):
    conn = get_db(client.app.state.data_dir / "knowledge.db")
    conn.executemany(
        "INSERT INTO laws(title, level, content_hash, status) VALUES(?,?,?,?)",
        [("无状态法规", "法律", "status-null", None),
         ("空白状态法规", "法律", "status-blank", "   ")])
    conn.commit()
    conn.close()

    stats = client.get("/api/stats").json()
    assert stats["laws"] == 3
    assert stats["by_status"] == {"现行有效": 1}
