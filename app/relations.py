# app/relations.py — LLM 关联图谱生成（法条间关系抽取 + 标题模糊定位入库）
# 用法：
#   python -m app.relations --law 1            # 对指定法规抽取
#   python -m app.relations --pending 5        # 处理尚未抽取的前 N 部法规
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.db import get_db, init_db
from app.digest import law_digest
from app.llm import LLMClient, LLMError

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

REL_TYPES = ("引用", "修订替代", "上位法", "同一事项", "程序衔接")

# failed 法规的最大自动重试次数（内容安全过滤等永久性拦截的法规不再无限烧 LLM 调用）
MAX_ATTEMPTS = 3

PROMPT = """你是证券法律法规专家。分析以下法规，抽取它与其他法律法规之间的关联关系。

法规标题：{title}
发文机关：{issuer}
条文摘要：
{digest}

只输出 JSON（不要其他文字），格式：
{{"relations": [
  {{"ref_title": "目标法规全名", "rel_type": "引用|修订替代|上位法|同一事项|程序衔接", "note": "一句话说明关联内容"}}
]}}
要求：
- ref_title 用规范的法规全名（如"中华人民共和国公司法"），不带书名号、不带版本号后缀
- 每个关系必须真实可靠，不确定的不要输出
- 最多输出 15 条最重要的关系"""


def _digest(db: sqlite3.Connection, law_id: int, max_chars: int = 1500) -> str:
    return law_digest(db, law_id, max_chars)


def extract_relations(llm: LLMClient, title: str, issuer: str, digest: str) -> list[dict]:
    """调用 LLM 抽取关系（结构化输出）。

    注意：不设 max_tokens —— 网关模型的思考过程(reasoning)与输出共用额度，
    设小会截断关系 JSON（实测 3000 会全部截空）。
    """
    msg = llm.chat([{"role": "user", "content": PROMPT.format(
        title=title, issuer=issuer or "未知", digest=digest)}])
    content = msg.get("content") or ""
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for r in data.get("relations", []):
        if (isinstance(r, dict) and r.get("ref_title") and r.get("rel_type") in REL_TYPES):
            out.append({"ref_title": str(r["ref_title"]).strip(),
                        "rel_type": r["rel_type"],
                        "note": str(r.get("note", ""))[:200]})
    return out


def _match_law(db: sqlite3.Connection, ref_title: str):
    """在库内按标题模糊定位目标法规。返回 law row 或 None。"""
    # 归一化：去书名号、去"关于修改《X》的决定"包装、去版本后缀
    t = ref_title.strip().strip("《》")
    m = re.search(r"修改[《「]?(.+?)[》》]?的决", ref_title)
    if m:
        t = m.group(1).strip("《》")
    candidates = [ref_title, t]
    core = re.sub(r"[（(][^）)]*[修订修正]{2}[^）)]*[）)]$", "", t).strip()
    if core and core != t:
        candidates.append(core)
    core_short = re.sub(r"^中华人民共和国", "", core) if core else ""
    if core_short and core_short != core:
        candidates.append(core_short)

    # 1) 精确匹配（含各变体）
    for c in candidates:
        if not c:
            continue
        row = db.execute("SELECT * FROM laws WHERE title = ?", (c,)).fetchone()
        if row:
            return row
    # 2) 包含匹配（短词优先，标题最短的优先，减少误匹配）
    for c in sorted([c for c in candidates if c], key=len):
        row = db.execute(
            "SELECT * FROM laws WHERE title LIKE ? ORDER BY length(title) LIMIT 1",
            (f"%{c}%",),
        ).fetchone()
        if row:
            return row
    return None


def _first_article(db: sqlite3.Connection, law_id: int):
    return db.execute(
        "SELECT id FROM articles WHERE law_id=? ORDER BY id LIMIT 1", (law_id,)
    ).fetchone()


def _mark_extraction(db: sqlite3.Connection, law_id: int, status: str,
                     extracted: int = 0, saved: int = 0, unmatched: int = 0,
                     note: str = "") -> None:
    """记录一次抽取尝试（upsert，attempts 递增）。pending 判定以此为准。"""
    db.execute(
        """INSERT INTO relation_extractions
             (law_id, status, attempts, extracted, saved, unmatched, note, attempted_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(law_id) DO UPDATE SET
             status=excluded.status,
             attempts=relation_extractions.attempts+1,
             extracted=excluded.extracted, saved=excluded.saved,
             unmatched=excluded.unmatched, note=excluded.note,
             attempted_at=excluded.attempted_at""",
        (law_id, status, 1, extracted, saved, unmatched,
         note[:200], datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()


def pending_laws(db: sqlite3.Connection, limit: int) -> list:
    """尚未完成关系抽取的法规（含 failed 未达重试上限的）。
    完成判定看 relation_extractions 标记，而非"是否有关系产出"——
    天然无关系的法规抽取一次即完成，不再被无限重试。"""
    return db.execute(
        """SELECT l.id, l.title FROM laws l
           WHERE NOT EXISTS (
             SELECT 1 FROM relation_extractions e WHERE e.law_id = l.id
               AND (e.status != 'failed' OR e.attempts >= ?))
           ORDER BY l.id LIMIT ?""",
        (MAX_ATTEMPTS, limit),
    ).fetchall()


def process_law(db: sqlite3.Connection, llm: LLMClient, law_id: int,
                digest_chars: int = 1500) -> dict:
    law = db.execute("SELECT * FROM laws WHERE id=?", (law_id,)).fetchone()
    if not law:
        return {"law_id": law_id, "error": "法规不存在"}
    from_art = _first_article(db, law_id)
    if not from_art:
        # 无法条的法规（如纯附件页）直接标记完成，避免无限重试
        _mark_extraction(db, law_id, "empty", note="该法规无法条")
        return {"law_id": law_id, "error": "该法规无法条"}
    try:
        rels = extract_relations(
            llm, law["title"], law["issuer"] or "",
            _digest(db, law_id, max_chars=digest_chars))
    except Exception as e:
        # 调用失败也记标记：attempts < MAX_ATTEMPTS 时下轮重试，达上限后放弃
        _mark_extraction(db, law_id, "failed", note=str(e))
        raise
    now = datetime.now().isoformat(timespec="seconds")
    saved = unmatched = 0
    for r in rels:
        target = _match_law(db, r["ref_title"])
        if not target or target["id"] == law_id:
            unmatched += 1
            continue
        to_art = _first_article(db, target["id"])
        if not to_art:
            unmatched += 1
            continue
        try:
            db.execute(
                """INSERT OR IGNORE INTO relations(from_id, to_id, rel_type, note, created_at)
                   VALUES(?,?,?,?,?)""",
                (from_art["id"], to_art["id"], r["rel_type"], r["note"], now),
            )
            saved += 1
        except sqlite3.IntegrityError:
            pass
    # 无论产出多少都标记完成（empty/ok），这是防死循环的关键
    _mark_extraction(db, law_id, "ok" if saved else "empty",
                     extracted=len(rels), saved=saved, unmatched=unmatched)
    return {"law_id": law_id, "title": law["title"], "extracted": len(rels),
            "saved": saved, "unmatched": unmatched}


def main():
    ap = argparse.ArgumentParser(description="关联图谱生成")
    ap.add_argument("--law", type=int, help="指定法规 id")
    ap.add_argument("--pending", type=int, help="处理尚未抽取的前 N 部法规")
    args = ap.parse_args()
    db = get_db(DATA_DIR / "knowledge.db")
    init_db(db)  # 幂等建表：旧库缺 relation_extractions 表时 pending_laws 会崩
    try:
        llm = LLMClient(load_config(DATA_DIR))
    except LLMError as e:
        # 未配置 LLM 时给一行明确指引退出，而非裸堆栈（Agent/双击场景友好）
        print(f"[relations] 无法启动：{e}")
        print("[relations] 配置方法：复制 config.example.json 为 data/config.json 并填写，或启动服务后在页面右上角齿轮配置")
        sys.exit(1)

    if args.law:
        ids = [args.law]
    elif args.pending:
        rows = pending_laws(db, args.pending)
        ids = [r["id"] for r in rows]
    else:
        print("请指定 --law <id> 或 --pending <N>")
        return
    for lid in ids:
        print(f"[relations] 处理法规 {lid} …", flush=True)
        try:
            print("[relations]", process_law(db, llm, lid))
        except Exception as e:
            # 单部法规失败不阻断（如网关内容安全过滤拦截敏感法规原文）
            print(f"[relations] 法规 {lid} 处理失败（跳过）: {str(e)[:200]}")


if __name__ == "__main__":
    main()
