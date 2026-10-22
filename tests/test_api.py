"""API 冒烟测试：应用工厂 + 临时数据库，不触碰生产数据。"""
import pytest
from fastapi.testclient import TestClient

from app.db import get_db, init_db
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as c:
        yield c


def test_health_and_stats(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["laws"] == 0
    assert client.get("/api/stats").json()["articles"] == 0


def test_law_list_detail_404(client):
    assert client.get("/api/laws").json()["total"] == 0
    assert client.get("/api/laws/999").status_code == 404


def test_settings_requires_no_key_leak(client):
    body = client.get("/api/settings").json()
    assert "api_key" not in body
    assert "api_key_masked" in body


def test_search_empty_query_422(client):
    assert client.get("/api/search").status_code == 422


def test_list_search_returns_200(client):
    assert client.get("/api/laws?q=证券").status_code == 200


@pytest.fixture
def seeded_client(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conn = get_db(data / "knowledge.db")
    init_db(conn)
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES('中华人民共和国证券法','法律','h1')")
    law_id = cur.lastrowid
    conn.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (law_id, "第五十条", "禁止利用内幕信息从事证券交易活动。"))
    cur = conn.execute(
        "INSERT INTO laws(title, level, content_hash, duplicate_of, quality_flag) "
        "VALUES('重复法规','法律','h2',?,'duplicate')", (law_id,))
    conn.execute(
        "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
        (cur.lastrowid, "第一条", "重复内容。"))
    conn.execute(
        "INSERT INTO laws(title, level, content_hash, quality_flag) "
        "VALUES('空法规','法律','h3','empty_text')")
    conn.commit()
    conn.close()
    app = create_app(data)
    with TestClient(app) as c:
        yield c


def test_list_search_hits_seg_index(seeded_client):
    r = seeded_client.get("/api/laws?q=内幕").json()
    assert r["total"] == 1
    assert r["items"][0]["title"] == "中华人民共和国证券法"


def test_list_default_excludes_duplicate_and_empty(seeded_client):
    r = seeded_client.get("/api/laws").json()
    titles = {x["title"] for x in r["items"]}
    assert titles == {"中华人民共和国证券法"}
