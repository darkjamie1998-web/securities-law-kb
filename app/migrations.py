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
    for target, _name, fn in MIGRATIONS:
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
