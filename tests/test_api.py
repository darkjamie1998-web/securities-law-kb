"""API 冒烟测试：应用工厂 + 临时数据库，不触碰生产数据。"""
import pytest
from fastapi.testclient import TestClient

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
