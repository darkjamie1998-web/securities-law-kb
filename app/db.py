# app/db.py — SQLite 连接、建表与 FTS5 同步触发器
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS laws (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    doc_number TEXT,
    issuer TEXT,
    level TEXT NOT NULL,
    topic_tags TEXT,
    issue_date TEXT,
    published_at TEXT,
    effective_date TEXT,
    status TEXT DEFAULT '现行有效',
    source_name TEXT,
    source_url TEXT,
    content_hash TEXT UNIQUE,
    full_text TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    law_id INTEGER NOT NULL REFERENCES laws(id),
    article_no TEXT,
    text TEXT NOT NULL,
    embedding TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_law ON articles(law_id);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY,
    from_id INTEGER NOT NULL REFERENCES articles(id),
    to_id INTEGER NOT NULL REFERENCES articles(id),
    rel_type TEXT NOT NULL,
    note TEXT,
    created_at TEXT,
    UNIQUE(from_id, to_id, rel_type)
);

CREATE TABLE IF NOT EXISTS wiki_pages (
    id INTEGER PRIMARY KEY,
    law_id INTEGER NOT NULL UNIQUE REFERENCES laws(id),
    markdown TEXT NOT NULL,
    model_used TEXT,
    generated_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY,
    source_name TEXT,
    started_at TEXT,
    finished_at TEXT,
    new_count INTEGER,
    updated_count INTEGER,
    status TEXT,
    detail TEXT
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    text, article_no,
    content='articles', content_rowid='id',
    tokenize='trigram'
);
"""

# articles 增删改时同步 FTS5（content 表模式必须手工维护）
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, text, article_no)
    VALUES (new.id, new.text, new.article_no);
END;

CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, text, article_no)
    VALUES ('delete', old.id, old.text, old.article_no);
END;

CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, text, article_no)
    VALUES ('delete', old.id, old.text, old.article_no);
    INSERT INTO articles_fts(rowid, text, article_no)
    VALUES (new.id, new.text, new.article_no);
END;
"""


def get_db(path) -> sqlite3.Connection:
    """打开 SQLite 连接：Row 行工厂 + 外键约束开启。path 可为 Path 或 ':memory:'。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表（幂等）。"""
    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA)
    conn.executescript(TRIGGERS)
    conn.commit()
