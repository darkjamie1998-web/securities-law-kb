# crawler/full_import.py — 首次全量入库（= 不带 limit 的 sync，翻页抓到底）
# 用法：python -m crawler.full_import --source 深圳证券交易所 [--limit 20]
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler import sync

if __name__ == "__main__":
    print("[full_import] 全量导入 = 无 limit 的 crawler.sync，直接转发")
    sync.main()
