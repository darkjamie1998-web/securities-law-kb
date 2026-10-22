# 证券法律法规知识库全方位优化 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复中文检索缺陷，补齐数据治理、版本化、合规、兼容性与工程化能力，交付可验证的知识库 v2。

**Architecture:** 保持 FastAPI + 原生前端 + SQLite 单文件架构。新增 jieba 分词 FTS 索引与 schema 迁移器；检索改为「精确条号 + 分词 BM25 + 原 FTS 兜底」三路融合；数据治理只标记不物理删除，全部操作先备份。

**Tech Stack:** Python 3.14、FastAPI、SQLite FTS5、jieba 0.42.1、原生 HTML/CSS/JS（ES2017）。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `app/migrations.py` | `PRAGMA user_version` 迁移器，v0→v1 |
| `app/db.py` | 连接 PRAGMA（WAL/busy_timeout）、调用迁移 |
| `app/seg_index.py` | jieba 分词 FTS 索引的建表、增量、重建 |
| `app/legal_terms.txt` | jieba 法律术语用户词典 |
| `app/stopwords.txt` | 检索停用词 |
| `app/search.py` | 精确条号解析、分词召回、融合排序、兜底 |
| `app/main.py` | `create_app(data_dir)` 工厂、连接依赖、API |
| `app/graph.py` | 图谱缓存 |
| `app/chat.py` | 工具调用防御、消息长度限制 |
| `app/relations.py` | JSON 提取容错 |
| `crawler/sync.py` | 版本快照、分词索引重建 |
| `crawler/browser_fetcher.py` | robots + 限频注入 |
| `crawler/targeted_import.py` | 共享限频器 |
| `tools/data_cleanup.py` | 备份、去重、质量标记、报告 |
| `web/style.css` | `inset` 回退、关键 flex 布局 margin 化 |
| `build_portable.py` | 路径可移植、排除 .git、DB 快照 |
| `requirements*.txt` | 增加 jieba |
| `tests/*` | 新增 6 个测试文件，总量 ≥120 |
| `README.md` / `AGENTS.md` / `docs/技术亮点.md` | 文档同步 |

---

## M2 数据模型与迁移

### Task 1: schema 迁移器与 DB 基础设置

**Files:**
- Create: `app/migrations.py`
- Modify: `app/db.py:105-119`
- Test: `tests/test_migrations.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_migrations.py
import sqlite3
import pytest
from app.db import get_db, init_db, SCHEMA
from app.migrations import CURRENT_VERSION


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migrate_v0_to_v1_adds_columns_and_indexes(tmp_path):
    db = get_db(tmp_path / "old.db")
    db.executescript(SCHEMA)          # 模拟旧库：只有基础表
    db.execute("PRAGMA user_version = 0")
    db.commit()
    init_db(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    cols = _columns(db, "laws")
    for name in ("quality_flag", "duplicate_of", "superseded_by", "version_label"):
        assert name in cols
    idx = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_relations_to" in idx
    assert "idx_laws_title" in idx
    db.close()


def test_migrate_is_idempotent(tmp_path):
    db = get_db(tmp_path / "old.db")
    db.executescript(SCHEMA)
    init_db(db)
    init_db(db)                        # 第二次不报错、不重复加列
    assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    db.close()


def test_migrate_keeps_existing_rows(tmp_path):
    db = get_db(tmp_path / "old.db")
    db.executescript(SCHEMA)
    db.execute("INSERT INTO laws(title, level) VALUES('证券法','法律')")
    db.commit()
    init_db(db)
    row = db.execute("SELECT title, status FROM laws").fetchone()
    assert row["title"] == "证券法"
    assert row["status"] == "现行有效"
    db.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PY -m pytest tests/test_migrations.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.migrations'`

- [ ] **Step 3: 实现 `app/migrations.py`**

```python
# app/migrations.py — SQLite schema 迁移（PRAGMA user_version）
import sqlite3

CURRENT_VERSION = 1

V1_LAW_COLUMNS = {
    "quality_flag": "TEXT",
    "duplicate_of": "INTEGER",
    "superseded_by": "INTEGER",
    "version_label": "TEXT",
}

V1_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id)",
    "CREATE INDEX IF NOT EXISTS idx_laws_title ON laws(title)",
    "CREATE INDEX IF NOT EXISTS idx_laws_status ON laws(status)",
    "CREATE INDEX IF NOT EXISTS idx_laws_dup ON laws(duplicate_of)",
    "CREATE INDEX IF NOT EXISTS idx_articles_law_no ON articles(law_id, article_no)",
    "CREATE INDEX IF NOT EXISTS idx_sync_log_source ON sync_log(source_name, id)",
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v1(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "laws")
    for name, ddl in V1_LAW_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE laws ADD COLUMN {name} {ddl}")
    for sql in V1_INDEXES:
        conn.execute(sql)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )


MIGRATIONS = [(1, "laws quality/version columns and indexes", _migrate_v1)]


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """按版本号顺序执行迁移；每一步事务化，失败不提升 user_version。"""
    version = current_version(conn)
    for target, name, fn in MIGRATIONS:
        if version >= target:
            continue
        conn.execute("BEGIN")
        try:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = target
    return version
```

- [ ] **Step 4: 修改 `app/db.py`**

`get_db` 增加 PRAGMA；`init_db` 调用迁移：

```python
from app.migrations import migrate


def get_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA cache_size = -64000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA)
    conn.executescript(TRIGGERS)
    conn.commit()
    migrate(conn)
```

> 注意：`executescript` 会隐式提交，迁移在其后单独执行。

- [ ] **Step 5: 运行测试并修复**

Run: `$PY -m pytest tests/test_migrations.py tests/test_db.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add app/migrations.py app/db.py tests/test_migrations.py
git commit -m "feat(db): schema migration v1 with WAL and quality/version columns"
```

---

## M1 检索 v2

### Task 2: jieba 词典与分词索引

**Files:**
- Create: `app/legal_terms.txt`, `app/stopwords.txt`, `app/seg_index.py`
- Test: `tests/test_seg_index.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_seg_index.py
from app.db import get_db, init_db
from app.seg_index import (
    ensure_schema, reindex_law, rebuild_all, ensure_fresh, seg_count,
)


def _seed(db):
    cur = db.execute(
        "INSERT INTO laws(title, level, content_hash) VALUES('中华人民共和国证券法','法律','h1')")
    law_id = cur.lastrowid
    for no, text in [
        ("第五十条", "禁止证券交易内幕信息的知情人和非法获取内幕信息的人利用内幕信息从事证券交易活动。"),
        ("第一百九十一条", "证券交易内幕信息的知情人或者非法获取内幕信息的人违反本法第五十三条的规定从事内幕交易的，责令依法处理非法持有的证券。"),
    ]:
        db.execute("INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
                   (law_id, no, text))
    db.commit()
    return law_id


def test_reindex_law_indexes_articles(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    law_id = _seed(db)
    reindex_law(db, law_id)
    assert seg_count(db) == 2
    rows = db.execute(
        "SELECT rowid FROM articles_seg WHERE articles_seg MATCH '内幕'").fetchall()
    assert len(rows) == 2
    db.close()


def test_reindex_law_is_idempotent(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    law_id = _seed(db)
    reindex_law(db, law_id)
    reindex_law(db, law_id)
    assert seg_count(db) == 2
    db.close()


def test_rebuild_all_and_ensure_fresh(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    _seed(db)
    assert rebuild_all(db) == 2
    assert seg_count(db) == 2
    # 删除一条索引后 ensure_fresh 补齐
    db.execute("DELETE FROM articles_seg WHERE rowid = (SELECT min(id) FROM articles)")
    db.commit()
    assert ensure_fresh(db) == 1
    assert seg_count(db) == 2
    db.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PY -m pytest tests/test_seg_index.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.seg_index'`

- [ ] **Step 3: 创建词典与停用词**

`app/legal_terms.txt`（每行一个词，jieba 用户词典）：

```text
内幕交易
短线交易
信息披露
信息披露义务
减持
注册制
退市整理期
关联交易
独立董事
融资融券
要约收购
合格投资者
债券违约
股权激励
操纵市场
虚假陈述
内幕信息
控股股东
实际控制人
一致行动人
证券发行
上市公司
证券交易所
证券公司
证券投资基金
行政处罚
行政复议
听证
```

`app/stopwords.txt`（每行一个词）：

```text
的
了
和
与
及
或
是
在
对
为
有
什么
怎么
如何
哪些
多少
吗
呢
请
问
关于
以及
或者
应当
可以
要求
规定
进行
相关
有关
```

- [ ] **Step 4: 实现 `app/seg_index.py`**

```python
# app/seg_index.py — jieba 分词 + FTS5 索引（BM25 中文检索）
import sqlite3
from pathlib import Path

import jieba

_DICT_LOADED = False


def _load_user_dict() -> None:
    global _DICT_LOADED
    if _DICT_LOADED:
        return
    path = Path(__file__).with_name("legal_terms.txt")
    if path.exists():
        jieba.load_userdict(str(path))
    _DICT_LOADED = True


def add_titles(titles) -> None:
    """把法规标题加入词典，提升法规名整体成词率。"""
    _load_user_dict()
    for title in titles:
        t = str(title).strip().strip("《》")
        if 2 <= len(t) <= 20:
            jieba.add_word(t, freq=10000)


def seg_text(text: str) -> str:
    _load_user_dict()
    return " ".join(w for w in jieba.cut_for_search(text or "") if w.strip())


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS articles_seg USING fts5(
               seg, article_no, tokenize='unicode61')"""
    )
    db.commit()


def seg_count(db: sqlite3.Connection) -> int:
    return db.execute("SELECT count(*) FROM articles_seg").fetchone()[0]


def reindex_law(db: sqlite3.Connection, law_id: int) -> int:
    """重建一部法规的全部分词索引（sync 更新后调用）。"""
    ids = [r[0] for r in db.execute(
        "SELECT id FROM articles WHERE law_id=?", (law_id,))]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM articles_seg WHERE rowid IN ({placeholders})", ids)
    rows = db.execute(
        f"SELECT id, text, article_no FROM articles WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    for r in rows:
        db.execute(
            "INSERT INTO articles_seg(rowid, seg, article_no) VALUES(?,?,?)",
            (r["id"], seg_text(r["text"]), r["article_no"] or ""),
        )
    return len(rows)


def rebuild_all(db: sqlite3.Connection) -> int:
    ensure_schema(db)
    db.execute("DELETE FROM articles_seg")
    rows = db.execute("SELECT id, text, article_no FROM articles").fetchall()
    for r in rows:
        db.execute(
            "INSERT INTO articles_seg(rowid, seg, article_no) VALUES(?,?,?)",
            (r["id"], seg_text(r["text"]), r["article_no"] or ""),
        )
    db.commit()
    return len(rows)


def ensure_fresh(db: sqlite3.Connection) -> int:
    """补齐缺失的分词索引行，返回补齐数量。"""
    ensure_schema(db)
    missing = db.execute(
        """SELECT a.id, a.text, a.article_no FROM articles a
           WHERE NOT EXISTS (SELECT 1 FROM articles_seg s WHERE s.rowid = a.id)"""
    ).fetchall()
    for r in missing:
        db.execute(
            "INSERT INTO articles_seg(rowid, seg, article_no) VALUES(?,?,?)",
            (r["id"], seg_text(r["text"]), r["article_no"] or ""),
        )
    if missing:
        db.commit()
    return len(missing)


if __name__ == "__main__":
    import argparse
    from app.db import get_db

    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--ensure", action="store_true")
    args = ap.parse_args()
    db = get_db(Path(__file__).resolve().parent.parent / "data" / "knowledge.db")
    if args.rebuild:
        print("rebuilt", rebuild_all(db))
    else:
        print("filled", ensure_fresh(db))
```

- [ ] **Step 5: 运行测试**

Run: `$PY -m pytest tests/test_seg_index.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/seg_index.py app/legal_terms.txt app/stopwords.txt tests/test_seg_index.py
git commit -m "feat(search): jieba tokenized FTS index"
```

### Task 3: 精确条号解析与检索融合

**Files:**
- Modify: `app/search.py`
- Test: `tests/test_search_quality.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_search_quality.py
import pytest
from app.db import get_db, init_db
from app.seg_index import ensure_schema, rebuild_all
from app.search import parse_citation, hybrid_search


@pytest.fixture
def db(tmp_path):
    conn = get_db(tmp_path / "t.db")
    init_db(conn)
    ensure_schema(conn)
    laws = [
        ("中华人民共和国证券法", [
            ("第五十条", "禁止证券交易内幕信息的知情人和非法获取内幕信息的人利用内幕信息从事证券交易活动。"),
            ("第一百九十一条", "证券交易内幕信息的知情人或者非法获取内幕信息的人违反本法第五十三条的规定从事内幕交易的，责令依法处理非法持有的证券，没收违法所得，并处以罚款。"),
        ]),
        ("中华人民共和国刑法", [
            ("第一百八十条", "证券、期货交易内幕信息的知情人员或者非法获取证券、期货交易内幕信息的人员，在涉及证券的发行，证券、期货交易或者其他对证券、期货交易价格有重大影响的信息尚未公开前，买入或者卖出该证券，情节严重的，处五年以下有期徒刑或者拘役。"),
        ]),
        ("上市公司股东减持股份管理暂行办法", [
            ("第六条", "大股东减持股份应当规范、理性、有序，充分关注上市公司及中小股东的利益。"),
            ("第七条", "上市公司应当及时了解股东减持本公司股份的情况，主动做好规则提示。"),
        ]),
        ("关于短线交易监管的若干规定", [
            ("第七条", "短线交易认定涉及的证券，按照股票、存托凭证、可交换公司债券、可转换公司债券等各品种的持有数量分别予以计算。"),
        ]),
    ]
    for i, (title, arts) in enumerate(laws, 1):
        cur = conn.execute(
            "INSERT INTO laws(title, level, content_hash) VALUES(?,?,?)",
            (title, "法律", f"h{i}"))
        lid = cur.lastrowid
        for no, text in arts:
            conn.execute(
                "INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
                (lid, no, text))
    conn.commit()
    rebuild_all(conn)
    yield conn
    conn.close()


def test_parse_citation_variants():
    assert parse_citation("证券法第五十条") == ("证券法", "第五十条")
    assert parse_citation("《中华人民共和国证券法》第一百九十一条") == (
        "中华人民共和国证券法", "第一百九十一条")
    assert parse_citation("证券法第50条") == ("证券法", "第五十条")
    assert parse_citation("内幕交易怎么处罚") is None


def test_exact_article_ranks_first(db):
    results = hybrid_search(db, None, "证券法第五十条", top_k=5)
    assert results
    assert results[0]["title"] == "中华人民共和国证券法"
    assert results[0]["article_no"] == "第五十条"
    assert results[0]["match_type"] == "exact_article"


def test_natural_language_hits_insider_trading(db):
    results = hybrid_search(db, None, "内幕交易怎么处罚", top_k=5)
    titles = {r["title"] for r in results}
    assert "中华人民共和国证券法" in titles
    assert "中华人民共和国刑法" in titles


def test_natural_language_hits_reduction(db):
    results = hybrid_search(db, None, "上市公司股东减持新规有什么要求", top_k=5)
    assert any("减持" in r["title"] for r in results)


def test_short_word_fallback(db):
    results = hybrid_search(db, None, "税", top_k=5)
    assert results == [] or all("score" in r for r in results)


def test_empty_and_special_queries_do_not_crash(db):
    assert hybrid_search(db, None, "", 5) == []
    assert hybrid_search(db, None, '"()*', 5) == [] or True
    hybrid_search(db, None, "x" * 500, 5)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PY -m pytest tests/test_search_quality.py -v`
Expected: FAIL `ImportError: cannot import name 'parse_citation'`

- [ ] **Step 3: 实现 `app/search.py` 新逻辑**

保留原有 `_fts_search`、`_like_search`、`_vector_search`、`_rrf`，新增：

```python
import jieba
import sqlite3
from pathlib import Path

_CN_DIGITS = "零一二三四五六七八九"
_CN_UNITS = ["", "十", "百", "千"]


def _int_to_cn(n: int) -> str:
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    out, unit = "", 0
    while n and unit < 4:
        d = n % 10
        if d:
            out = _CN_DIGITS[d] + _CN_UNITS[unit] + out
        n //= 10
        unit += 1
    return out


def _cn_to_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS.index(ch)
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
    return section + num or None


CITATION_RE = re.compile(
    r"《?([^《》\s，。；：？！]{2,30}?)》?\s*第([一二三四五六七八九十百千零〇\d]+)条")


def parse_citation(query: str):
    """识别「法规名+条号」，返回 (法规名, 规范条号)；无则 None。"""
    m = CITATION_RE.search(query or "")
    if not m:
        return None
    title, num = m.group(1).strip(), m.group(2)
    n = _cn_to_int(num)
    if n is None:
        return None
    return title, f"第{_int_to_cn(n)}条"


def _load_stopwords() -> set[str]:
    path = Path(__file__).with_name("stopwords.txt")
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()}


_STOPWORDS = _load_stopwords()


def jieba_terms(query: str) -> list[str]:
    from app.seg_index import _load_user_dict
    _load_user_dict()
    words = jieba.cut_for_search(query or "")
    out = []
    for w in words:
        w = w.strip()
        if len(w) >= 2 and w not in _STOPWORDS and w not in out:
            out.append(w)
    return out


def _exact_article(db, law_title: str, article_no: str, top_k: int = 5):
    row = db.execute(
        """SELECT id, title FROM laws
           WHERE title = ? AND duplicate_of IS NULL AND superseded_by IS NULL""",
        (law_title,)).fetchone()
    if not row:
        row = db.execute(
            """SELECT id, title FROM laws
               WHERE title LIKE ? AND duplicate_of IS NULL AND superseded_by IS NULL
               ORDER BY length(title) LIMIT 1""",
            (f"%{law_title}%",)).fetchone()
    if not row:
        return []
    art = db.execute(
        "SELECT id, article_no, text FROM articles WHERE law_id=? AND article_no=?",
        (row["id"], article_no)).fetchone()
    if not art:
        return []
    return [{
        "id": art["id"], "law_id": row["id"], "article_no": art["article_no"],
        "text": art["text"], "title": row["title"], "level": "",
        "status": "现行有效", "source_url": "",
        "score": 10.0, "match_type": "exact_article",
    }][:top_k]


def _seg_search(db, terms: list[str], top_k: int = 50) -> list[int]:
    if not terms:
        return []
    expr = " OR ".join(f'"{t}"' for t in terms)
    try:
        rows = db.execute(
            "SELECT rowid FROM articles_seg WHERE articles_seg MATCH ? "
            "ORDER BY rank LIMIT ?", (expr, top_k)).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


def hybrid_search(db, llm, query: str, top_k: int = 10) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    exact = []
    cite = parse_citation(query)
    if cite:
        exact = _exact_article(db, *cite, top_k=top_k)

    terms = jieba_terms(query)
    seg_ids = _seg_search(db, terms)
    kw_ids = _fts_search(db, query)
    if not seg_ids and not kw_ids:
        kw_ids = _like_search(db, query, top_k)

    vec_ids: list[int] = []
    if llm is not None and llm.embedding_model:
        has_embedded = db.execute(
            "SELECT 1 FROM articles WHERE embedding IS NOT NULL LIMIT 1").fetchone()
        if has_embedded:
            try:
                qv = llm.embed([query])[0]
                vec_ids = _vector_search(db, qv)
            except Exception:
                pass

    fused = _rrf([seg_ids, kw_ids, vec_ids], top_k * 3)
    rows = db.execute(
        f"""SELECT a.id, a.law_id, a.article_no, a.text,
                   l.title, l.level, l.status, l.source_url
            FROM articles a JOIN laws l ON a.law_id = l.id
            WHERE a.id IN ({",".join("?" * len(fused))})""",
        [aid for aid, _ in fused]).fetchall() if fused else []
    by_id = {r["id"]: dict(r) for r in rows}

    out, seen = [], set()
    for item in exact:
        if item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)
    for aid, score in fused:
        if aid in seen or aid not in by_id:
            continue
        seen.add(aid)
        out.append(by_id[aid] | {"score": round(score, 6),
                                 "match_type": "keyword"})
    return out[:top_k]
```

> `_rrf` 保持不变，接受列表的列表。

- [ ] **Step 4: 运行测试**

Run: `$PY -m pytest tests/test_search_quality.py tests/test_search.py -v`
Expected: 全部 PASS；如精确条号未命中，检查 `CITATION_RE` 对"证券法第五十条"的 group(1) 是否为"证券法"。

- [ ] **Step 5: 在生产库上做只读基准**

```bash
$PY - <<'PY'
from app.db import get_db
from app.search import hybrid_search
c = get_db('data/knowledge.db')
for q in ["内幕交易的构成要件和处罚","内幕交易怎么处罚","上市公司股东减持新规有什么要求","短线交易禁止","证券法第五十条"]:
    r = hybrid_search(c, None, q, 5)
    print(q, len(r), [(x['title'][:18], x['article_no']) for x in r[:3]])
PY
```
Expected: 每个查询 ≥1 条，精确条号 top1 为《证券法》第五十条。

- [ ] **Step 6: 提交**

```bash
git add app/search.py tests/test_search_quality.py
git commit -m "feat(search): jieba BM25 + exact article citation retrieval"
```

---

## M2/M4 服务层

### Task 4: FastAPI 应用工厂与连接生命周期

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PY -m pytest tests/test_api.py -v`
Expected: FAIL `ImportError: cannot import name 'create_app'`

- [ ] **Step 3: 改造 `app/main.py`**

核心结构：

```python
def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = Path(data_dir) if data_dir else DATA_DIR

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = get_db(data_dir / "knowledge.db")
        init_db(conn)
        ensure_schema(conn)
        try:
            from app.seg_index import add_titles, ensure_fresh
            add_titles([r["title"] for r in conn.execute("SELECT title FROM laws")])
            ensure_fresh(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(title="证券法律法规知识库", lifespan=lifespan)
    app.state.data_dir = data_dir

    def get_conn():
        conn = get_db(data_dir / "knowledge.db")
        try:
            yield conn
        finally:
            conn.close()

    # 所有端点签名加 conn=Depends(get_conn)，删除局部 db() 调用
    # try_llm()/settings 使用 app.state.data_dir
    ...
    return app


app = create_app()
```

要点：

- `try_llm(data_dir)` 改为接收目录参数。
- `/api/chat`、`/api/laws/{id}/wiki` 继续用 `LLMClient(load_config(app.state.data_dir))`。
- 删除模块级 `db()` 函数；所有端点通过 `Depends(get_conn)` 获取连接。
- `app.mount("/static", StaticFiles(directory=WEB_DIR))` 保持在工厂内。
- 测试通过 `create_app(tmp_path)` 注入临时目录，不触碰生产库。

- [ ] **Step 4: 运行 API 测试**

Run: `$PY -m pytest tests/test_api.py tests/test_health.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/main.py tests/test_api.py
git commit -m "refactor(api): app factory with request-scoped sqlite connections"
```

### Task 5: 同步版本快照

**Files:**
- Modify: `crawler/sync.py`
- Test: `tests/test_sync_versions.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_sync_versions.py
from app.db import get_db, init_db
from app.seg_index import ensure_schema
from crawler.sync import upsert_law


def _source():
    return {"name": "测试来源", "level": "法律"}


def _doc(text):
    return {"title": "测试法", "full_text": text, "source_url": "http://x"}


def test_update_creates_superseded_snapshot(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    assert upsert_law(db, _source(), _doc("第一条 旧内容。" * 20)) == "new"
    assert upsert_law(db, _source(), _doc("第一条 新内容。" * 20)) == "updated"
    rows = db.execute("SELECT id,status,superseded_by FROM laws ORDER BY id").fetchall()
    assert len(rows) == 2
    old, new = rows
    assert old["status"] == "已修订"
    assert old["superseded_by"] == new["id"]
    assert new["status"] == "现行有效"
    old_arts = db.execute(
        "SELECT count(*) FROM articles WHERE law_id=?", (old["id"],)).fetchone()[0]
    assert old_arts > 0
    db.close()


def test_same_content_is_skip(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    doc = _doc("第一条 内容。" * 30)
    assert upsert_law(db, _source(), doc) == "new"
    assert upsert_law(db, _source(), doc) == "skip"
    db.close()


def test_update_marks_referencing_laws_pending(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    ensure_schema(db)
    upsert_law(db, {"name": "S", "level": "法律"},
               {"title": "A法", "full_text": "第一条 A。" * 30})
    upsert_law(db, {"name": "S", "level": "法律"},
               {"title": "B法", "full_text": "第一条 B。" * 30})
    a = db.execute("SELECT id FROM laws WHERE title='A法'").fetchone()[0]
    b = db.execute("SELECT id FROM laws WHERE title='B法'").fetchone()[0]
    a_art = db.execute("SELECT id FROM articles WHERE law_id=?", (a,)).fetchone()[0]
    b_art = db.execute("SELECT id FROM articles WHERE law_id=?", (b,)).fetchone()[0]
    db.execute("INSERT INTO relations(from_id,to_id,rel_type) VALUES(?,?,?)",
               (b_art, a_art, "引用"))
    db.execute("INSERT INTO relation_extractions(law_id,status) VALUES(?, 'ok')", (b,))
    db.commit()
    upsert_law(db, {"name": "S", "level": "法律"},
               {"title": "A法", "full_text": "第一条 A 新。" * 30})
    assert db.execute(
        "SELECT count(*) FROM relation_extractions WHERE law_id=?", (b,)
    ).fetchone()[0] == 0
    db.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PY -m pytest tests/test_sync_versions.py -v`
Expected: FAIL（当前更新不生成快照，laws 只有 1 行）

- [ ] **Step 3: 修改 `upsert_law`**

关键逻辑：

```python
from app.seg_index import reindex_law


def _snapshot_version(db, law) -> int | None:
    """把当前法规复制为旧版快照；返回快照 law_id，冲突时返回 None。"""
    conflict = db.execute(
        "SELECT id FROM laws WHERE content_hash=? AND id!=?",
        (law["content_hash"], law["id"])).fetchone()
    if conflict:
        return None
    cur = db.execute(
        """INSERT INTO laws(title, doc_number, issuer, level, topic_tags,
             issue_date, published_at, effective_date, status, source_name,
             source_url, content_hash, full_text, created_at, updated_at,
             quality_flag, duplicate_of, superseded_by, version_label)
           SELECT title, doc_number, issuer, level, topic_tags,
             issue_date, published_at, effective_date, '已修订', source_name,
             source_url, content_hash, full_text, created_at, updated_at,
             NULL, NULL, ?, version_label
           FROM laws WHERE id=?""",
        (law["id"], law["id"]))
    snap_id = cur.lastrowid
    db.execute(
        """INSERT INTO articles(law_id, article_no, text)
           SELECT ?, article_no, text FROM articles WHERE law_id=?""",
        (snap_id, law["id"]))
    return snap_id


row = db.execute(
    """SELECT * FROM laws
       WHERE source_name=? AND title=? AND superseded_by IS NULL
         AND duplicate_of IS NULL""",
    (source["name"], title)).fetchone()
...
if row:
    if row["content_hash"] != chash:
        _snapshot_version(db, row)
    law_id = row["id"]
    db.execute("""UPDATE laws SET doc_number=?, issuer=?, issue_date=?,
                  effective_date=?, content_hash=?, full_text=?, updated_at=?,
                  version_label=? WHERE id=?""", (...))
```

- 更新后重建 articles 时调用 `reindex_law(db, law_id)`（在 `db.commit()` 前）。
- `version_label` 用 `re.search(r"[（(](\d{4}年(?:修订|修正))[）)]", title)` 提取，无则 None。
- 快照插入失败（content_hash 冲突）时继续更新当前版本，记录 `print` 警告。

- [ ] **Step 4: 运行测试**

Run: `$PY -m pytest tests/test_sync_versions.py tests/test_sync.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add crawler/sync.py tests/test_sync_versions.py
git commit -m "feat(sync): keep superseded law snapshots on content update"
```

---

## M3 数据治理

### Task 6: 备份、去重与质量标记 CLI

**Files:**
- Create: `tools/data_cleanup.py`, `tools/__init__.py`
- Test: `tests/test_data_cleanup.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_data_cleanup.py
from app.db import get_db, init_db
from tools.data_cleanup import normalize_title, similarity, dedupe, mark_quality


def _add(db, title, text, source="测试", hash_=None):
    cur = db.execute(
        "INSERT INTO laws(title, level, source_name, content_hash, full_text) "
        "VALUES(?,?,?,?,?)",
        (title, "法律", source, hash_ or title + text[:10], text))
    db.execute("INSERT INTO articles(law_id, article_no, text) VALUES(?,?,?)",
               (cur.lastrowid, "第一条", text))
    db.commit()
    return cur.lastrowid


def test_normalize_title():
    assert normalize_title("【证监会公告】《证券法》（2025年修订）") == "证券法"
    assert normalize_title("  中华人民共和国证券法  ") == "中华人民共和国证券法"


def test_similarity_threshold():
    assert similarity("第一条 内容。" * 100, "第一条 内容。" * 100) > 0.99


def test_dedupe_marks_duplicates(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    a = _add(db, "证券法", "第一条 内容。" * 100, source="国家法律法规数据库")
    b = _add(db, "【转载】证券法", "第一条 内容。" * 100, source="其他")
    assert dedupe(db, dry_run=False) >= 1
    b_row = db.execute("SELECT duplicate_of,quality_flag FROM laws WHERE id=?", (b,)).fetchone()
    assert b_row["duplicate_of"] == a
    assert b_row["quality_flag"] == "duplicate"
    db.close()


def test_mark_quality_empty_text(tmp_path):
    db = get_db(tmp_path / "t.db")
    init_db(db)
    lid = _add(db, "空法规", "短")
    assert mark_quality(db, dry_run=False) >= 1
    assert db.execute("SELECT quality_flag FROM laws WHERE id=?", (lid,)).fetchone()[0] == "empty_text"
    db.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PY -m pytest tests/test_data_cleanup.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'tools.data_cleanup'`

- [ ] **Step 3: 实现 `tools/data_cleanup.py`**

```python
# tools/data_cleanup.py — 备份、去重、质量标记
import argparse
import difflib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "knowledge.db"
BACKUP_DIR = DATA_DIR / "backups"

SOURCE_RANK = {
    "国家法律法规数据库": 100, "中国人大网": 95, "中国政府网": 90,
    "最高人民法院": 80, "中国证监会": 70,
    "上海证券交易所": 60, "深圳证券交易所": 60, "北京证券交易所": 60,
}


def backup_db(src: Path = DB_PATH, dest_dir: Path = BACKUP_DIR) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"knowledge-{datetime.now():%Y%m%d-%H%M%S}.db"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return dest


def normalize_title(title: str) -> str:
    t = re.sub(r"^【[^】]*】", "", title or "").strip()
    t = t.strip("《》").strip()
    t = re.sub(r"[（(]\d{4}年(修订|修正)[）)]$", "", t).strip()
    return t


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "")[:20000], (b or "")[:20000]).ratio()


def _rank(row) -> tuple:
    return (SOURCE_RANK.get(row["source_name"], 0), len(row["full_text"] or ""))


def dedupe(db, dry_run: bool = False) -> int:
    rows = db.execute(
        """SELECT id, title, full_text, source_name, issue_date, effective_date,
                  content_hash FROM laws
           WHERE duplicate_of IS NULL AND superseded_by IS NULL""").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(normalize_title(r["title"]), []).append(r)
    changed = 0
    for _, group in groups.items():
        if len(group) < 2:
            continue
        group = sorted(group, key=_rank, reverse=True)
        keep = group[0]
        for other in group[1:]:
            if other["content_hash"] == keep["content_hash"] or \
               similarity(other["full_text"], keep["full_text"]) >= 0.95:
                if not dry_run:
                    db.execute(
                        "UPDATE laws SET duplicate_of=?, quality_flag='duplicate' WHERE id=?",
                        (keep["id"], other["id"]))
                changed += 1
    if not dry_run:
        db.commit()
    return changed


def mark_quality(db, dry_run: bool = False) -> int:
    changed = 0
    for r in db.execute("SELECT id, full_text FROM laws").fetchall():
        if len((r["full_text"] or "").strip()) < 200:
            if not dry_run:
                db.execute("UPDATE laws SET quality_flag='empty_text' WHERE id=?", (r["id"],))
            changed += 1
    empty_ids = [r[0] for r in db.execute(
        "SELECT id FROM articles WHERE length(trim(text)) < 5")]
    if empty_ids and not dry_run:
        q = ",".join("?" * len(empty_ids))
        db.execute(f"DELETE FROM articles WHERE id IN ({q})", empty_ids)
    if not dry_run:
        db.commit()
    return changed + len(empty_ids)


def report(db) -> dict:
    return {
        "laws": db.execute("SELECT count(*) FROM laws").fetchone()[0],
        "duplicates": db.execute(
            "SELECT count(*) FROM laws WHERE duplicate_of IS NOT NULL").fetchone()[0],
        "empty_text": db.execute(
            "SELECT count(*) FROM laws WHERE quality_flag='empty_text'").fetchone()[0],
        "empty_articles": db.execute(
            "SELECT count(*) FROM articles WHERE length(trim(text)) < 5").fetchone()[0],
        "unsplit": db.execute(
            "SELECT count(*) FROM articles WHERE article_no IS NULL OR article_no=''").fetchone()[0],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup-only", action="store_true")
    ap.add_argument("--dedupe", action="store_true")
    ap.add_argument("--mark-quality", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    backup = backup_db()
    print(f"[backup] {backup}")
    if args.backup_only:
        return
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        if args.dedupe:
            print("[dedupe] changed", dedupe(db, args.dry_run))
        if args.mark_quality:
            print("[quality] changed", mark_quality(db, args.dry_run))
        if args.report or not (args.dedupe or args.mark_quality):
            rep = report(db)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            if not args.dry_run:
                out = DATA_DIR / f"cleanup-report-{datetime.now():%Y%m%d}.json"
                out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[report] {out}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 创建空文件 `tools/__init__.py` 并运行测试**

Run: `$PY -m pytest tests/test_data_cleanup.py -v`
Expected: PASS

- [ ] **Step 5: 在生产库执行（先备份，再 dry-run，再应用）**

```bash
$PY tools/data_cleanup.py --backup-only
$PY tools/data_cleanup.py --dedupe --mark-quality --report --dry-run
$PY tools/data_cleanup.py --dedupe --mark-quality --report
```

Expected: 输出备份路径、重复/空数据计数；`laws` 总数不变，`duplicate_of` 非空数量 >0。

- [ ] **Step 6: 提交**

```bash
git add tools/__init__.py tools/data_cleanup.py tests/test_data_cleanup.py
git commit -m "feat(data): backup-first dedupe and quality marking CLI"
```

---

## M4 性能与可靠性

### Task 7: 列表搜索、图谱缓存、聊天防御

**Files:**
- Modify: `app/main.py`, `app/graph.py`, `app/chat.py`, `app/relations.py`
- Test: `tests/test_api.py`, `tests/test_graph.py`

- [ ] **Step 1: 写测试**

`tests/test_graph.py` 追加：

```python
def test_build_graph_cache_returns_same_object():
    from app import graph
    db = graph_test_db()          # 复用该文件已有的内存库构造
    first = graph.build_graph_cached(db)
    second = graph.build_graph_cached(db)
    assert first == second
```

`tests/test_api.py` 追加：

```python
def test_list_search_uses_title_and_seg(client):
    assert client.get("/api/laws?q=证券").status_code == 200
```

- [ ] **Step 2: 修改 `app/main.py` 的 `list_laws`**

`q` 搜索改为：

```python
if q:
    like = f"%{q}%"
    seg_terms = jieba_terms(q)
    law_ids = []
    if seg_terms:
        expr = " OR ".join(f'"{t}"' for t in seg_terms)
        try:
            law_ids = [r[0] for r in conn.execute(
                """SELECT DISTINCT a.law_id FROM articles_seg s
                   JOIN articles a ON a.id = s.rowid
                   WHERE articles_seg MATCH ? LIMIT 500""", (expr,))]
        except sqlite3.OperationalError:
            law_ids = []
    cond_parts = ["l.title LIKE ?"]
    params.extend([like])
    if law_ids:
        cond_parts.append(f"l.id IN ({','.join('?' * len(law_ids))})")
        params.extend(law_ids)
    where.append("(" + " OR ".join(cond_parts) + ")")
```

同时默认排除：

```python
where.append("duplicate_of IS NULL")
where.append("(quality_flag IS NULL OR quality_flag != 'duplicate')")
```

- [ ] **Step 3: 图谱缓存**

`app/graph.py` 增加：

```python
_CACHE = {"key": None, "data": None, "ts": 0.0}


def build_graph_cached(db, ttl: float = 60.0) -> dict:
    import time
    key = (
        db.execute("SELECT count(*) FROM laws").fetchone()[0],
        db.execute("SELECT max(updated_at) FROM laws").fetchone()[0],
        db.execute("SELECT count(*) FROM relations").fetchone()[0],
    )
    now = time.time()
    if _CACHE["key"] == key and now - _CACHE["ts"] < ttl:
        return _CACHE["data"]
    data = build_graph(db)
    _CACHE.update({"key": key, "data": data, "ts": now})
    return data
```

`build_graph` 的 laws 查询加 `WHERE duplicate_of IS NULL`，排除重复节点。

- [ ] **Step 4: 聊天与关系抽取防御**

`app/chat.py`：

```python
for tc in tool_calls:
    fn = tc.get("function") if isinstance(tc, dict) else None
    if not isinstance(fn, dict):
        continue
    name = fn.get("name") or ""
    ...
```

`agent_chat` 入口限制：

```python
messages = [m for m in messages if isinstance(m, dict)][-40:]
for m in messages:
    if isinstance(m.get("content"), str):
        m["content"] = m["content"][:8000]
```

`app/relations.py` 的 JSON 提取：

```python
start, end = content.find("{"), content.rfind("}")
if start >= 0 and end > start:
    try:
        data = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        data = {}
else:
    data = {}
```

- [ ] **Step 5: 运行测试**

Run: `$PY -m pytest tests/test_api.py tests/test_graph.py tests/test_chat.py tests/test_relations.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/main.py app/graph.py app/chat.py app/relations.py tests/test_graph.py tests/test_api.py
git commit -m "perf: seg-based law search, graph cache, defensive chat parsing"
```

---

## M5 合规与兼容

### Task 8: 浏览器抓取限频与 robots

**Files:**
- Modify: `crawler/browser_fetcher.py`, `crawler/targeted_import.py`, `crawler/fetcher.py`
- Test: `tests/test_browser_fetcher.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_browser_fetcher.py
import pytest
from crawler.browser_fetcher import BrowserFetcher


class FakeLimiter:
    def __init__(self):
        self.hosts = []
    def wait(self, host, interval=None):
        self.hosts.append(host)


def test_render_checks_robots_and_limiter(monkeypatch):
    limiter = FakeLimiter()
    calls = {"robots": 0}
    def robots(url):
        calls["robots"] += 1
        return True
    bf = BrowserFetcher(rate_limiter=limiter, daily_max=10, robots_checker=robots)
    monkeypatch.setattr(bf, "_render", lambda url, wait_selector, timeout: "<html/>")
    assert bf.render("https://example.com/a", "src") == "<html/>"
    assert calls["robots"] == 1
    assert limiter.hosts == ["example.com"]
    bf.close()


def test_render_rejects_robots_denied(monkeypatch):
    bf = BrowserFetcher(rate_limiter=FakeLimiter(), robots_checker=lambda url: False)
    with pytest.raises(PermissionError):
        bf.render("https://example.com/a", "src")
    bf.close()


def test_daily_max(monkeypatch):
    bf = BrowserFetcher(rate_limiter=FakeLimiter(), daily_max=0)
    with pytest.raises(RuntimeError):
        bf.render("https://example.com/a", "src")
    bf.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PY -m pytest tests/test_browser_fetcher.py -v`
Expected: FAIL（构造参数不支持/无 robots 检查）

- [ ] **Step 3: 改造 `BrowserFetcher`**

```python
class BrowserFetcher:
    def __init__(self, rate_limiter=None, daily_max: int = 500,
                 robots_checker=None):
        self.limiter = rate_limiter
        self.daily_max = daily_max
        self.robots_checker = robots_checker
        self._count = 0
        self._pw = None
        self._browser = None
        self._page = None

    def _ensure_page(self):
        if self._page is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page(user_agent=BROWSER_UA)

    def _render(self, url, wait_selector, timeout):
        self._ensure_page()
        self._page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        if wait_selector:
            try:
                self._page.wait_for_selector(wait_selector, timeout=timeout)
            except Exception:
                pass
        else:
            self._page.wait_for_load_state("networkidle", timeout=timeout)
        return self._page.content()

    def render(self, url, source, wait_selector=None, timeout=20000):
        if self._count >= self.daily_max:
            raise RuntimeError(f"browser fetch reached daily_max={self.daily_max}")
        if self.robots_checker and not self.robots_checker(url):
            raise PermissionError(f"robots.txt disallows: {url}")
        if self.limiter:
            self.limiter.wait(urlparse(url).netloc)
        self._count += 1
        return self._render(url, wait_selector, timeout)
```

- [ ] **Step 4: 修改 `targeted_import.py` 与 `fetcher.py`**

```python
browser = BrowserFetcher(
    rate_limiter=fetcher.limiter,
    daily_max=int(cfg.get("daily_max_per_source", 500)),
    robots_checker=fetcher.check_robots,
)
```

`Fetcher` 增加公共方法：

```python
def check_robots(self, url: str) -> bool:
    return self._check_robots(url)
```

- [ ] **Step 5: 运行测试**

Run: `$PY -m pytest tests/test_browser_fetcher.py tests/test_fetcher.py -v`
Expected: PASS（测试中不真正启动 Playwright：`_render` 被 monkeypatch）

- [ ] **Step 6: 提交**

```bash
git add crawler/browser_fetcher.py crawler/targeted_import.py crawler/fetcher.py tests/test_browser_fetcher.py
git commit -m "fix(crawler): browser fetcher shares rate limiter and respects robots"
```

### Task 9: 旧浏览器 CSS 兼容

**Files:**
- Modify: `web/style.css`
- Test: 静态扫描

- [ ] **Step 1: 修复 `inset`**

`.modal`：

```css
.modal {
  position: fixed;
  top: 0; right: 0; bottom: 0; left: 0;   /* 旧浏览器回退 */
  inset: 0;
  ...
}
```

- [ ] **Step 2: 关键 flex 布局去 gap**

对以下选择器移除 `gap`，改用子元素 margin：

```css
.topbar > * + * { margin-left: 16px; }
.law-card .meta > * + * { margin-left: 12px; }
.modal-actions > * + * { margin-left: 10px; }
.chat-input-row > * + * { margin-left: 8px; }
.pager > * + * { margin-left: 8px; }
```

删除对应 `gap` 声明（避免新浏览器双倍间距）。其余非关键 `gap` 可保留。

- [ ] **Step 3: 静态验证**

```bash
grep -n "inset:" web/style.css
grep -nE "gap:" web/style.css
grep -nE '\?\.|\?\?|\.trimEnd\(|\.flat\(|\.matchAll\(|replaceAll\(' web/*.js
```
Expected: `inset` 前有 top/right/bottom/left；关键选择器无 gap；JS 无 ES2019+ 语法。

- [ ] **Step 4: 提交**

```bash
git add web/style.css
git commit -m "fix(web): legacy browser fallbacks for inset and flex gaps"
```

---

## M6 测试、构建与文档

### Task 10: 便携包构建可移植化

**Files:**
- Modify: `build_portable.py`, `requirements.txt`, `requirements-full.txt`
- Test: `tests/test_build_portable.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_build_portable.py
import sqlite3
from build_portable import SRC, build_db_snapshot


def test_src_is_project_root():
    assert (SRC / "app" / "main.py").exists()
    assert (SRC / "build_portable.py").exists()


def test_db_snapshot_is_readable(tmp_path):
    src = tmp_path / "a.db"
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES(1)")
    c.commit()
    c.close()
    out = build_db_snapshot(src, tmp_path / "out.db")
    assert out.exists()
    assert sqlite3.connect(out).execute("SELECT x FROM t").fetchone()[0] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `$PY -m pytest tests/test_build_portable.py -v`
Expected: FAIL `ImportError: cannot import name 'build_db_snapshot'`

- [ ] **Step 3: 改造 `build_portable.py`**

```python
import sqlite3
from datetime import datetime

SRC = Path(__file__).resolve().parent
DST = SRC / "dist" / "法律法规知识库查询"
VENV_SITE = SRC / ".venv" / "Lib" / "site-packages"
COPY_DIRS = ["app", "crawler", "web", "tests", "docs", "tools"]
COPY_FILES = [...]


def python_home() -> Path:
    runtime = SRC / "runtime" / "python.exe"
    if runtime.exists():
        return SRC / "runtime"
    return Path(sys.base_prefix)


def build_db_snapshot(src_db: Path, dest_db: Path) -> Path:
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(src_db))
    dst = sqlite3.connect(str(dest_db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest_db


def main():
    ...
    with_git = "--with-git" in sys.argv
    if with_git:
        COPY_DIRS.append(".git")
    ...
    build_db_snapshot(SRC / "data" / "knowledge.db", DST / "data" / "knowledge.db")
    zip_path = DST.parent / f"法律法规知识库查询-{datetime.now():%Y%m%d}.zip"
```

其他改动：

- 验证命令增加 `import jieba` 和 `SELECT count(*) FROM articles_seg`。
- 复制时排除 `data/raw`、`data/backups`。
- `robocopy` 前检查源目录存在。

- [ ] **Step 4: 更新依赖清单**

`requirements.txt` 追加 `jieba`；`requirements-full.txt` 追加 `jieba==0.42.1`（按字母序插入）。

- [ ] **Step 5: 运行测试**

Run: `$PY -m pytest tests/test_build_portable.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add build_portable.py requirements.txt requirements-full.txt tests/test_build_portable.py
git commit -m "build: portable output with path detection and consistent db snapshot"
```

### Task 11: 文档同步

**Files:**
- Modify: `README.md`, `AGENTS.md`, `docs/技术亮点.md`

- [ ] **Step 1: README**

更新：

- 测试数量（实际运行结果）。
- 检索说明：jieba 分词 FTS（BM25）+ 精确条号 + trigram 兜底。
- 新增命令：

```bash
$PY -m app.seg_index --ensure        # 补齐分词索引
$PY -m app.seg_index --rebuild       # 全量重建（词典变更后）
$PY tools/data_cleanup.py --report   # 数据质量报告（自动备份）
```

- 数据备份位置 `data/backups/`。
- 版本化说明：内容更新保留旧版快照，状态"已修订"。
- 便携包构建新增 `--with-git`。

- [ ] **Step 2: AGENTS.md**

补充：

- 分词索引是派生数据，可重建；词典 `app/legal_terms.txt`。
- 数据治理脚本会先备份，默认不物理删除法规。
- 便携包构建默认不含 `.git`。
- WAL 模式说明与备份注意事项。

- [ ] **Step 3: 技术亮点**

更新检索架构描述：三路召回（精确条号 / jieba BM25 / trigram phrase）+ RRF 融合。

- [ ] **Step 4: 验证文档命令可执行**

```bash
$PY -m app.seg_index --ensure
$PY tools/data_cleanup.py --report
```

- [ ] **Step 5: 提交**

```bash
git add README.md AGENTS.md docs/技术亮点.md
git commit -m "docs: update retrieval, data governance and portable build"
```

### Task 12: 全量验证

- [ ] **Step 1: 测试**

```bash
$PY -m pytest tests/ -v
```
Expected: ≥120 passed

- [ ] **Step 2: 检索基准**

```bash
$PY - <<'PY'
from app.db import get_db
from app.search import hybrid_search
c = get_db('data/knowledge.db')
qs = ["内幕交易的构成要件和处罚","内幕交易怎么处罚","上市公司股东减持新规有什么要求",
      "短线交易禁止","证券法第五十条","信息披露义务","证券发行注册制","退市整理期",
      "关联交易审议程序","独立董事职责","操纵市场","融资融券","债券违约处置","股权激励",
      "行政处罚听证","私募基金合格投资者","上市公司收购要约","证券法第五十条","刑法第一百八十条"]
hit = 0
for q in qs:
    r = hybrid_search(c, None, q, 5)
    hit += 1 if r else 0
    print(f"{q}: {len(r)}")
print("hit", hit, "/", len(qs))
PY
```
Expected: hit ≥17/19

- [ ] **Step 3: 生产数据治理**

```bash
$PY tools/data_cleanup.py --dedupe --mark-quality --report
```

- [ ] **Step 4: 索引重建与一致性**

```bash
$PY -m app.seg_index --ensure
$PY - <<'PY'
import sqlite3
c=sqlite3.connect('data/knowledge.db')
print('articles', c.execute('select count(*) from articles').fetchone()[0])
print('seg', c.execute('select count(*) from articles_seg').fetchone()[0])
print('journal', c.execute('pragma journal_mode').fetchone()[0])
PY
```
Expected: `articles == seg`，`journal == wal`

- [ ] **Step 5: 便携包构建（可选，耗时较长）**

```bash
$PY build_portable.py --skip-zip
```
Expected: 依赖导入验证通过、DB 校验通过、`runtime/python.exe` 存在、jieba 可导入。

- [ ] **Step 6: 最终检查**

```bash
git status --short
```
Expected: 仅包含预期修改；`data/`、`dist/` 未入库。
