# run_relations_forever.py — 图谱全量生成长跑脚本（独立于会话，可中断重跑）
# 用法：python run_relations_forever.py [--batch 50] [--sleep 5] [--stuck-minutes 45]
# 每批处理 --batch 部未抽取法规，直到全部完成；中断后重跑自动续。
#
# 可靠性设计（2026-09-23 修复，起因：上一实例卡死 22 小时无进展）：
# - 实例锁：msvcrt 文件锁，进程无论怎么退出（被杀/崩溃）锁都自动释放
# - 看门狗：后台线程检测"长时间无产出"后 os._exit，避免半挂死状态
# - 完成标记：relation_extractions 表记录每部法规的抽取结果，
#   天然无关系的法规抽取一次即完成，不再无限重试烧 LLM 调用
# - 失败重试上限：failed 法规最多自动重试 MAX_ATTEMPTS 次（内容过滤拦截的不再烧钱）
import argparse
import atexit
import msvcrt
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_config
from app.db import get_db, init_db
from app.llm import LLMClient
from app.relations import process_law, pending_laws, MAX_ATTEMPTS

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOCK_PATH = DATA_DIR / "relations_forever.lock"


def acquire_instance_lock():
    """独占文件锁：同机只能跑一个实例；进程死亡时 OS 自动释放。"""
    DATA_DIR.mkdir(exist_ok=True)
    f = open(LOCK_PATH, "a+")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(f"[exit] 已有实例在运行（锁: {LOCK_PATH}），本进程退出。")
        f.close()
        sys.exit(1)
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    f.flush()

    def _release():
        # 锁定的是位置 0 的 1 字节：解锁前必须先回到 0，否则对未锁区域
        # 解锁会抛 PermissionError（traceback 污染正常退出日志）
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()

    atexit.register(_release)
    return f


class Watchdog:
    """卡死看门狗：超过 stuck_minutes 无活动则强杀本进程（配合外部重启）。"""

    def __init__(self, stuck_minutes: int):
        self.limit = stuck_minutes * 60
        self.last_activity = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="watchdog")

    def feed(self):
        self.last_activity = time.time()

    def _run(self):
        while not self._stop.wait(30):
            idle = time.time() - self.last_activity
            if idle > self.limit:
                print(f"[watchdog] {idle/60:.0f} 分钟无活动，强制退出（请检查网关状态）",
                      flush=True)
                os._exit(3)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()


def backfill_extraction_marks(db: sqlite3.Connection) -> int:
    """把历史"已有关系产出"的法规回填完成标记（幂等，仅首次跑新逻辑时生效）。"""
    cur = db.execute(
        """INSERT OR IGNORE INTO relation_extractions
             (law_id, status, attempts, extracted, saved, note, attempted_at)
           SELECT DISTINCT a.law_id, 'ok', 1, 0, 0,
                  '历史回填（升级前已有关系产出）',
                  datetime('now', 'localtime')
           FROM relations r JOIN articles a ON a.id = r.from_id""")
    db.commit()
    return cur.rowcount


def pending_count(db: sqlite3.Connection) -> int:
    return len(pending_laws(db, 10 ** 9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--sleep", type=int, default=5, help="批间隔秒数")
    ap.add_argument("--stuck-minutes", type=int, default=45,
                    help="看门狗：连续无产出多少分钟后强制退出")
    args = ap.parse_args()

    lock = acquire_instance_lock()  # noqa: F841 — 持引用防 GC，锁随进程生命周期
    db = get_db(DATA_DIR / "knowledge.db")
    init_db(db)  # 幂等建表（含 relation_extractions）
    n = backfill_extraction_marks(db)
    if n:
        print(f"[migrate] 回填 {n} 部历史已抽取法规的完成标记", flush=True)
    llm = LLMClient(load_config(DATA_DIR))
    wd = Watchdog(args.stuck_minutes)
    wd.start()

    batch_no = 0
    consecutive_gateway_hangs = 0  # 连续硬超时（网关"滴字节"挂起特征）
    while True:
        rows = pending_laws(db, args.batch)
        if not rows:
            print(f"[done] {datetime.now():%H:%M:%S} 全部法规处理完成！")
            return
        batch_no += 1
        print(f"[batch {batch_no}] {datetime.now():%H:%M:%S} "
              f"本批 {len(rows)} 部，剩余 {pending_count(db)} 部未处理", flush=True)
        wd.feed()
        ok = fail = 0
        for r in rows:
            try:
                try:
                    result = process_law(db, llm, r["id"])
                except Exception as first_err:
                    # 超时类失败：用极短 digest 降级重试一次（超长法规如证券法）
                    is_timeout = ("timed out" in str(first_err)
                                  or "硬超时" in str(first_err))
                    if not is_timeout:
                        raise
                    print(f"  [{r['id']}] {r['title'][:30]} 超时，降级重试（短摘要）...",
                          flush=True)
                    result = process_law(db, llm, r["id"], digest_chars=400)
                print(f"  [{r['id']}] {result.get('title', r['title'])[:30]} "
                      f"extracted={result.get('extracted')} saved={result.get('saved')}",
                      flush=True)
                ok += 1
                consecutive_gateway_hangs = 0
            except Exception as e:
                # 内容过滤/持续超时等：标记 failed；attempts < MAX_ATTEMPTS 时下轮重试
                print(f"  [{r['id']}] {r['title'][:30]} 失败"
                      f"（重试至多 {MAX_ATTEMPTS} 次后放弃）: {str(e)[:120]}",
                      flush=True)
                fail += 1
                # 网关持续挂起时线程池会被泄漏 worker 占满，后续 submit 只能排队
                # 空转（每部 2×660s 但不烧 LLM）。连续 3 次硬超时 = 网关病了，
                # 直接退出让外部重启机制决定何时再来，不空耗一天墙钟
                if "硬超时" in str(e):
                    consecutive_gateway_hangs += 1
                    if consecutive_gateway_hangs >= 3:
                        print("[exit] 连续 3 次硬超时，网关疑似持续挂起，"
                              "退出等待人工/外部重启", flush=True)
                        return
            wd.feed()  # 每部法规处理完都喂狗（无论成败，只要在动就没卡死）
        print(f"[batch {batch_no}] 完成 ok={ok} fail={fail}", flush=True)
        if ok == 0:
            # 整批无进展（如网关持续超时/故障）：退出避免死循环，稍后重跑即可续
            print("[exit] 本批全部失败，可能网关异常，稍后重跑本脚本续跑", flush=True)
            return
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
