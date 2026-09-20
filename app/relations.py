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
from app.db import get_db
from app.llm import LLMClient

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

REL_TYPES = ("引用", "修订替代", "上位法", "同一事项", "程序衔接")

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
    """条文摘要：全文超长时取首部+尾部（缩短以控制推理耗时与超时）。"""
    text = db.execute(
        "SELECT full_text FROM laws WHERE id=?", (law_id,)
    ).fetchone()["full_text"] or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars * 2 // 3] + "\n……（中略）……\n" + text[-max_chars // 3:]


def extract_relations(llm: LLMClient, title: str, issuer: str, digest: str) -> list[dict]:
    """调用 LLM 抽取关系（结构化输出）。

    注意：不设 max_tokens —— 网关模型的思考过程(reasoning)与输出共用额度，
    设小会截断关系 JSON（实测 3000 会全部截空）。
    """
    msg = llm.chat([{"role": "user", "content": PROMPT.format(
        title=title, issuer=issuer or "未知", digest=digest)}])
    content = msg["content"] or ""
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


def _has_relations(db: sqlite3.Connection, law_id: int) -> bool:
    r = db.execute(
        """SELECT 1 FROM relations r JOIN articles a ON a.id = r.from_id
           WHERE a.law_id = ? LIMIT 1""",
        (law_id,),
    ).fetchone()
    return r is not None


def process_law(db: sqlite3.Connection, llm: LLMClient, law_id: int) -> dict:
    law = db.execute("SELECT * FROM laws WHERE id=?", (law_id,)).fetchone()
    if not law:
        return {"law_id": law_id, "error": "法规不存在"}
    rels = extract_relations(
        llm, law["title"], law["issuer"] or "", _digest(db, law_id))
    now = datetime.now().isoformat(timespec="seconds")
    saved = unmatched = 0
    from_art = _first_article(db, law_id)
    if not from_art:
        return {"law_id": law_id, "error": "该法规无法条"}
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
    db.commit()
    return {"law_id": law_id, "title": law["title"], "extracted": len(rels),
            "saved": saved, "unmatched": unmatched}


def main():
    ap = argparse.ArgumentParser(description="关联图谱生成")
    ap.add_argument("--law", type=int, help="指定法规 id")
    ap.add_argument("--pending", type=int, help="处理尚未抽取的前 N 部法规")
    args = ap.parse_args()
    db = get_db(DATA_DIR / "knowledge.db")
    llm = LLMClient(load_config(DATA_DIR))

    if args.law:
        ids = [args.law]
    elif args.pending:
        rows = db.execute(
            """SELECT l.id FROM laws l
               WHERE NOT EXISTS (
                 SELECT 1 FROM relations r JOIN articles a ON a.id = r.from_id
                 WHERE a.law_id = l.id)
               ORDER BY l.id LIMIT ?""",
            (args.pending,),
        ).fetchall()
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
