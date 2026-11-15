"""测试统一入口：隔离路径与运行期配置。

后端模块使用裸导入（如 ``import group_config``），日历是 ``app`` 包，
因此这里把两个目录都加入 sys.path。

日历服务在导入时就会打开 SQLite（位于模块级），测试必须指向临时库，
不能碰生产环境的 /config/snh48.db。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "backend" / "app", ROOT / "calendar"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_TEMP_DIR = Path(tempfile.mkdtemp(prefix="box48-tests-"))
os.environ["CONFIG_PATH"] = str(_TEMP_DIR / "roster.yaml")
os.environ["ROSTER_PATH"] = str(_TEMP_DIR / "roster.yaml")
os.environ["SCHEDULE_DB"] = str(_TEMP_DIR / "snh48.db")
