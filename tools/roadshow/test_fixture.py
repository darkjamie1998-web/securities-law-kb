"""精选离线 fixture 的黑盒一致性验收。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tools.roadshow import extract_fixture


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = Path(__file__).with_name("fixture.json")
DB_URI = f"file:{(ROOT / 'data' / 'knowledge.db').as_posix()}?mode=ro"
LAW_IDS = {20, 215, 216, 293, 296, 302, 406, 820}
REQUIRED_EDGES = {
    (215, 20),
    (216, 20),
    (293, 20),
    (293, 216),
    (302, 216),
    (406, 20),
    (820, 20),
}
DISCLAIMER = "精选离线演示，具体事项需核对官方最新原文"


@pytest.fixture(scope="module")
def data() -> dict:
    if not FIXTURE_PATH.is_file():
        pytest.fail("fixture.json 尚未生成，请先运行 tools.roadshow.extract_fixture")
    raw = FIXTURE_PATH.read_bytes()
    assert raw.decode("utf-8").encode("utf-8") == raw, "fixture 必须是无 BOM 的 UTF-8"
    return json.loads(raw)


@pytest.fixture(scope="module")
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_URI, uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_selected_laws_and_real_metadata(data: dict, db: sqlite3.Connection) -> None:
    # JSON 对象键只能是字符串，读取后显式转成 int 再与固定 ID 集合比较。
    assert {int(law_id) for law_id in data["laws"]} == LAW_IDS
    assert len(data["laws"]) >= 8
    for law_id, law in data["laws"].items():
        row = db.execute("SELECT * FROM laws WHERE id = ?", (int(law_id),)).fetchone()
        assert row is not None
        assert law["id"] == int(law_id)
        for key in ("title", "level", "status", "source_name", "source_url"):
            assert law[key] == row[key]


def test_article_selection_and_complete_text(data: dict, db: sqlite3.Connection) -> None:
    assert {int(law_id) for law_id in data["articles"]} == LAW_IDS
    assert len(data["articles"]["20"]) >= 50
    assert len(data["articles"]["20"]) == 60
    expected_first_60 = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM articles WHERE law_id = 20 ORDER BY id LIMIT 60"
        )
    ]
    assert [a["id"] for a in data["articles"]["20"]] == expected_first_60

    for law_id in (215, 296):
        selected = data["articles"][str(law_id)]
        assert selected
        assert all("减持" in article["text"] for article in selected)
        expected_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM articles WHERE law_id = ? AND instr(text, '减持') > 0 "
                "ORDER BY id",
                (law_id,),
            )
        ]
        assert [article["id"] for article in selected] == expected_ids

    for law_id in (216, 293, 302, 406, 820):
        expected_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM articles WHERE law_id = ? ORDER BY id", (law_id,)
            )
        ]
        assert [a["id"] for a in data["articles"][str(law_id)]] == expected_ids

    assert data["laws"]["820"]["quality_flag"] == "unsplit"
    assert len(data["articles"]["820"]) == 1
    assert data["articles"]["820"][0]["text"] == db.execute(
        "SELECT text FROM articles WHERE law_id = 820"
    ).fetchone()["text"]

    for articles in data["articles"].values():
        for article in articles:
            row = db.execute(
                "SELECT law_id, article_no, text FROM articles WHERE id = ?",
                (article["id"],),
            ).fetchone()
            assert article["law_id"] == row["law_id"]
            assert article["article_no"] == row["article_no"]
            assert article["text"] == row["text"], "单条正文不得截断或清洗"


def test_article_by_id_uses_string_keys_and_same_records(data: dict) -> None:
    expected = {
        str(article["id"]): article
        for articles in data["articles"].values()
        for article in articles
    }
    assert set(data["article_by_id"]) == set(expected)
    assert all(isinstance(key, str) for key in data["article_by_id"])
    for article_id, article in expected.items():
        assert data["article_by_id"][article_id] == article
    assert "9349" in data["article_by_id"]


def test_relations_are_real_and_graph_is_complete(data: dict, db: sqlite3.Connection) -> None:
    db_relations = {
        row["id"]: dict(row)
        for row in db.execute(
            """SELECT r.id, r.from_id, r.to_id, r.rel_type, r.note,
                      fa.law_id AS from_law_id, ta.law_id AS to_law_id
               FROM relations r
               JOIN articles fa ON fa.id = r.from_id
               JOIN articles ta ON ta.id = r.to_id"""
        )
    }
    assert data["relations"]
    for relation in data["relations"]:
        source = db_relations[relation["id"]]
        for key, value in source.items():
            assert relation[key] == value
        assert relation["from_law_id"] in LAW_IDS
        assert relation["to_law_id"] in LAW_IDS

    edges = {(edge["s"], edge["t"]) for edge in data["graph"]["edges"]}
    assert REQUIRED_EDGES <= edges
    assert any(e["s"] == 293 and e["t"] == 216 for e in data["graph"]["edges"])
    assert {node["id"] for node in data["graph"]["nodes"]} == LAW_IDS
    for node in data["graph"]["nodes"]:
        assert node["title"] == data["laws"][str(node["id"])]["title"]
        assert node["level"] == data["laws"][str(node["id"])]["level"]
        assert node["status"] == data["laws"][str(node["id"])]["status"]
        assert isinstance(node["board"], str) and node["board"]
        assert isinstance(node["out_deg"], int) and node["out_deg"] >= 0
        assert isinstance(node["in_deg"], int) and node["in_deg"] >= 0


def test_stats_is_separate_full_library_snapshot(data: dict) -> None:
    stats = data["stats"]
    assert stats["scope"] == "full_library_snapshot"
    assert stats["laws"] > len(data["laws"])
    assert stats["articles"] > sum(len(items) for items in data["articles"].values())
    assert stats["total_laws"] >= stats["laws"]
    assert "curated" not in stats["scope"]


def test_preset_chats_and_citation_compatibility(data: dict) -> None:
    assert len(data["chats"]) == 2
    all_citations = []
    for chat in data["chats"].values():
        assert chat["question"]
        assert DISCLAIMER in chat["content"]
        assert chat["searches"]
        assert chat["citations"]
        for citation in chat["citations"]:
            all_citations.append(citation)
            assert citation["law_id"] in LAW_IDS
            assert str(citation["article_id"]) in data["article_by_id"]
            article = data["article_by_id"][str(citation["article_id"])]
            law = data["laws"][str(citation["law_id"])]
            assert citation["article_no"] == article["article_no"]
            assert citation["title"] == law["title"]
            assert citation["source_url"] == law["source_url"]
            assert citation["ref_title"]
            assert citation["ref_no"] == citation["article_no"]
            assert f"【{citation['ref_title']}·{citation['ref_no']}】" in chat["content"]

    expected = {
        (9349, 215, "第九条"),
        (13717, 293, "第四条"),
        (13721, 293, "第八条"),
    }
    assert {
        (c["article_id"], c["law_id"], c["article_no"]) for c in all_citations
    } == expected


def test_wiki_is_pregenerated_grounded_markdown(data: dict) -> None:
    assert "293" in data["wikis"]
    wiki = data["wikis"]["293"]
    assert wiki["law_id"] == 293
    markdown = wiki["markdown"]
    assert markdown.startswith("# ")
    assert "## " in markdown
    assert "第四条" in markdown and "第八条" in markdown
    assert DISCLAIMER in markdown
    assert "实时" not in (wiki.get("model_used") or "")


class _FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self.payload


def test_stats_transport_and_invalid_json_are_controlled(monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise extract_fixture.urllib.error.URLError("offline")

    monkeypatch.setattr(extract_fixture.urllib.request, "urlopen", unavailable)
    with pytest.raises(RuntimeError, match="NEEDS_CONTEXT.*api/stats"):
        extract_fixture.fetch_stats()

    monkeypatch.setattr(
        extract_fixture.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse(b"{not-json"),
    )
    with pytest.raises(RuntimeError, match="NEEDS_CONTEXT.*api/stats"):
        extract_fixture.fetch_stats()


@pytest.mark.parametrize(
    "stats, missing_field",
    [
        ({}, "laws"),
        ({"laws": 853}, "articles"),
        ({"laws": 853, "articles": 21065}, "total_laws"),
        ({"laws": 853, "articles": "21065", "total_laws": 879}, "articles"),
    ],
)
def test_stats_required_fields_are_validated(monkeypatch, stats, missing_field) -> None:
    monkeypatch.setattr(
        extract_fixture.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse(json.dumps(stats).encode("utf-8")),
    )
    with pytest.raises(RuntimeError, match=f"NEEDS_CONTEXT.*{missing_field}"):
        extract_fixture.fetch_stats()


def test_main_stats_failure_is_fatal(monkeypatch, capsys) -> None:
    def fail_stats():
        raise RuntimeError("NEEDS_CONTEXT: 模拟 Stats 不可用")

    monkeypatch.setattr(extract_fixture, "fetch_stats", fail_stats)
    assert extract_fixture.main() == 2
    assert "FATAL NEEDS_CONTEXT: 模拟 Stats 不可用" in capsys.readouterr().err


def test_missing_selected_law_is_controlled() -> None:
    class EmptyRows:
        def fetchall(self):
            return []

    class MissingLawConnection:
        def execute(self, *args):
            return EmptyRows()

    with pytest.raises(RuntimeError, match="精选法规缺失.*20"):
        extract_fixture.extract_laws(MissingLawConnection())


@pytest.mark.parametrize("article_id", [9349, 13717, 13721])
def test_missing_fixed_citation_is_controlled(article_id: int) -> None:
    with pytest.raises(RuntimeError, match=f"固定引用条文缺失.*{article_id}"):
        extract_fixture.make_citation({}, {}, article_id, "测试法规")
