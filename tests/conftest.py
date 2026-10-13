import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试库随手就重建，不需要真落盘：共享盘/容器里 fsync 往往要 1 秒以上，
# 关掉同步写能让整套测试从几十秒降到几秒（生产环境别设这个，默认是 NORMAL）。
os.environ.setdefault("EMSCAN_SQLITE_SYNC", "OFF")
