# app/digest.py — LLM 输入用的法规全文摘要（超长取首尾，控制推理耗时）
import sqlite3


def law_digest(db: sqlite3.Connection, law_id: int, max_chars: int = 1500) -> str:
    """条文摘要：全文超长时取首 2/3 + 尾 1/3（中略标记）。

    relations（默认 1500 字）与 wiki（默认 6000 字）共用此实现。
    """
    row = db.execute("SELECT full_text FROM laws WHERE id=?", (law_id,)).fetchone()
    text = (row["full_text"] if row else "") or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars * 2 // 3] + "\n……（中略）……\n" + text[-max_chars // 3:]
